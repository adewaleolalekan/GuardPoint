"""Client for the NIPOST Postcode Gateway.

One interface, two transports:

* :class:`LiveTransport` calls ``https://api.postcode.gov.ng`` with an
  ``X-API-Key`` header. Selected automatically when ``POSTCODE_API_KEY`` is set.
* :class:`SimulatedTransport` serves the same shapes from a local dataset so the
  whole product can be built, demoed and tested without a key or credits.

Both honour the documented lookup levels (L1-L5, cumulative) and the documented
error envelope, so callers never need to know which is in play.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import Settings, settings as default_settings
from .postcodes import Segments, normalise, parse

DATA_FILE = Path(__file__).resolve().parent / "data" / "postcodes.json"


class PostcodeError(Exception):
    """An error returned by (or standing in for) the gateway."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass
class LookupResult:
    """Normalised result of a graded lookup, independent of transport."""

    postcode: str
    valid: bool
    level_served: int
    state_name: str | None = None
    lga_name: str | None = None
    locality_name: str | None = None
    zone: str | None = None
    recent_house_address: str | None = None
    building_use_status: str | None = None
    other_building_info: dict[str, Any] | None = None
    point_geometry: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def administrative_address(self) -> dict[str, Any] | None:
        if not (self.state_name or self.lga_name or self.locality_name):
            return None
        return {
            "state_name": self.state_name,
            "lga_name": self.lga_name,
            "locality_name": self.locality_name,
            "zone": self.zone,
        }


class Transport(Protocol):
    def lookup(self, code: str, level: int) -> LookupResult: ...
    def autocomplete(self, query: str) -> dict[str, Any]: ...
    def reverse(self, lng: float, lat: float, max_distance_m: float) -> dict[str, Any]: ...
    def nearby(self, lng: float, lat: float, radius: float) -> dict[str, Any]: ...
    def states(self) -> list[dict[str, str]]: ...
    def lgas(self, state: str) -> list[dict[str, str]]: ...


# --------------------------------------------------------------------------- #
# Live transport
# --------------------------------------------------------------------------- #


class LiveTransport:
    """Talks to the real gateway. Never used without an API key."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx  # imported lazily so the simulator needs no network stack

        url = f"{self._base}{path}"
        try:
            resp = httpx.get(
                url,
                params=params,
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:  # network/DNS/timeout
            raise PostcodeError("gateway_unreachable", f"could not reach the gateway: {exc}", 502) from exc

        if resp.status_code >= 400:
            body: dict[str, Any] = {}
            try:
                body = resp.json()
            except ValueError:
                pass
            err = body.get("error", {})
            raise PostcodeError(
                err.get("code", f"http_{resp.status_code}"),
                err.get("message", resp.text or "gateway error"),
                resp.status_code,
            )
        return resp.json()

    def lookup(self, code: str, level: int) -> LookupResult:
        payload = self._get("/v1/lookup", {"code": code, "level": level})
        return _result_from_live(payload.get("data", {}), level)

    def autocomplete(self, query: str) -> dict[str, Any]:
        return self._get("/v1/search/autocomplete", {"q": query}).get("data", {})

    def reverse(self, lng: float, lat: float, max_distance_m: float) -> dict[str, Any]:
        return self._get(
            "/v1/search/reverse",
            {"lng": lng, "lat": lat, "max_distance_m": max_distance_m},
        ).get("data", {})

    def nearby(self, lng: float, lat: float, radius: float) -> dict[str, Any]:
        return self._get("/v1/search/nearby", {"lng": lng, "lat": lat, "radius": radius}).get("data", {})

    def states(self) -> list[dict[str, str]]:
        return self._get("/v1/reference/states", {}).get("data", {}).get("states", [])

    def lgas(self, state: str) -> list[dict[str, str]]:
        return self._get("/v1/reference/lgas", {"state": state}).get("data", {}).get("lgas", [])


def _result_from_live(data: dict[str, Any], requested_level: int) -> LookupResult:
    admin = data.get("administrative_address") or {}
    recent = data.get("recent_house_address") or {}
    return LookupResult(
        postcode=normalise(data.get("postcode", "")) if data.get("postcode") else "",
        valid=bool(data.get("valid", False)),
        level_served=requested_level,
        state_name=admin.get("state_name"),
        lga_name=admin.get("lga_name"),
        locality_name=admin.get("locality_name"),
        zone=admin.get("zone"),
        recent_house_address=recent.get("recent") if isinstance(recent, dict) else recent,
        building_use_status=data.get("building_use_status"),
        other_building_info=data.get("other_building_info"),
        point_geometry=data.get("point_geometry"),
        raw=data,
    )


# --------------------------------------------------------------------------- #
# Simulated transport
# --------------------------------------------------------------------------- #


class SimulatedTransport:
    """Serves the documented shapes from a bundled dataset.

    Faithful to the parts that matter for integration work: cumulative lookup
    levels, the 250m reverse-geocode ceiling, the error envelope, and the fact
    that L2+ needs a lookup level the caller is granted. It also meters credits
    the way the real gateway does, so a product can be costed before going live.
    """

    def __init__(self, max_level: int = 3) -> None:
        self._max_level = max_level
        self._lock = threading.Lock()
        self.credits_spent = 0
        raw = json.loads(DATA_FILE.read_text())
        self._states: list[dict[str, str]] = raw["states"]
        self._lgas: dict[str, list[dict[str, str]]] = raw["lgas"]
        self._units: list[dict[str, Any]] = raw["units"]
        self._by_code = {u["postcode"]: u for u in self._units}

    # -- helpers ---------------------------------------------------------- #

    def _meter(self, level: int) -> None:
        # L1 is free; L2+ costs credits. The exact schedule is commercial, so
        # we use a simple, documented-in-README stand-in: 1 credit per level
        # above L1.
        if level > 1:
            with self._lock:
                self.credits_spent += level - 1

    def _require_level(self, level: int) -> None:
        if level > self._max_level:
            raise PostcodeError(
                "level_not_granted",
                f"your key is not granted lookup level {level}",
                403,
            )

    # -- API surface ------------------------------------------------------ #

    def lookup(self, code: str, level: int) -> LookupResult:
        self._require_level(level)
        try:
            canonical = normalise(code)
        except ValueError as exc:
            raise PostcodeError("invalid_postcode", str(exc), 400) from exc

        self._meter(level)
        unit = self._by_code.get(canonical)
        if unit is None:
            # Valid structure, but not a building we know about.
            return LookupResult(postcode=canonical, valid=False, level_served=level)

        result = LookupResult(
            postcode=canonical,
            valid=True,
            level_served=level,
        )
        if level >= 2:
            result.state_name = unit["state_name"]
            result.lga_name = unit["lga_name"]
            result.locality_name = unit["locality_name"]
            result.zone = unit["zone"]
            result.recent_house_address = unit["recent_house_address"]
        if level >= 3:
            result.building_use_status = unit["building_use_status"]
        if level >= 4:
            result.other_building_info = unit["other_building_info"]
        if level >= 5:
            lng, lat = unit["coordinates"]
            result.point_geometry = {"type": "Point", "coordinates": [lng, lat]}
        return result

    def autocomplete(self, query: str) -> dict[str, Any]:
        q = (query or "").strip().upper()
        if not q:
            raise PostcodeError("invalid_query", "q is required", 400)
        compact = q.replace(" ", "").replace("-", "")
        matches = [
            u
            for u in self._units
            if u["postcode"].replace("-", "").startswith(compact)
        ]
        if not matches:
            matches = [
                u for u in self._units if compact[:2] == u["postcode"][:2]
            ]
        segment = ["state", "lga", "district", "area", "unit"][min(len(compact) // 2, 4)]
        return {
            "segment": segment,
            "suggestions": [
                {"code": u["postcode"], "label": u["recent_house_address"]} for u in matches[:8]
            ],
        }

    def reverse(self, lng: float, lat: float, max_distance_m: float) -> dict[str, Any]:
        radius = max(0.0, min(float(max_distance_m or 25), 250.0))
        best: tuple[float, dict[str, Any]] | None = None
        for unit in self._units:
            ulng, ulat = unit["coordinates"]
            distance = _haversine_m(lng, lat, ulng, ulat)
            if distance <= radius and (best is None or distance < best[0]):
                best = (distance, unit)
        if best is None:
            return {
                "found": False,
                "coordinate": [lng, lat],
                "radius_m": radius,
                "message": "no postcode within range",
            }
        distance, unit = best
        confidence = "high" if distance <= 25 else "medium" if distance <= 120 else "low"
        seg: Segments = parse(unit["postcode"])
        return {
            "found": True,
            "coordinate": [lng, lat],
            "radius_m": radius,
            "unit": {
                "postcode": unit["postcode"],
                "display": seg.display(),
                "distance_m": round(distance, 1),
                "confidence": confidence,
                "state_name": unit["state_name"],
                "lga_name": unit["lga_name"],
                "locality_name": unit["locality_name"],
                "address": unit["recent_house_address"],
            },
            "area": f"{seg.state}-{seg.lga}-{seg.district}-{seg.area}",
            "district": f"{seg.state}-{seg.lga}-{seg.district}",
            "state": seg.state,
        }

    def nearby(self, lng: float, lat: float, radius: float) -> dict[str, Any]:
        radius = float(radius or 300)
        results = []
        for unit in self._units:
            ulng, ulat = unit["coordinates"]
            distance = _haversine_m(lng, lat, ulng, ulat)
            if distance <= radius:
                results.append(
                    {
                        "postcode": unit["postcode"],
                        "distance_m": round(distance, 1),
                        "point_geometry": {
                            "type": "Point",
                            "coordinates": unit["coordinates"],
                        },
                    }
                )
        results.sort(key=lambda r: r["distance_m"])
        return {"results": results}

    def states(self) -> list[dict[str, str]]:
        return list(self._states)

    def lgas(self, state: str) -> list[dict[str, str]]:
        code = (state or "").upper()
        if code not in self._lgas:
            raise PostcodeError("invalid_state", f"unknown state '{state}'", 400)
        return list(self._lgas[code])


def _haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #


class PostcodeClient:
    """Thin facade the rest of the app depends on."""

    def __init__(self, transport: Transport, max_level: int) -> None:
        self._transport = transport
        self.max_level = max_level

    @property
    def transport_name(self) -> str:
        return type(self._transport).__name__

    def lookup(self, code: str, level: int = 1) -> LookupResult:
        return self._transport.lookup(code, level)

    def lookup_graded(self, code: str, min_level: int) -> LookupResult:
        """Look up at ``min_level``, degrading gracefully if not granted.

        Returns the richest result the granted level allows, so a caller that
        asked for L3 still gets L1 validity when only L1 is granted rather than
        a hard failure. The served level is reported on the result.
        """
        requested = min(min_level, self.max_level)
        while requested >= 1:
            try:
                return self._transport.lookup(code, requested)
            except PostcodeError as exc:
                if exc.code == "level_not_granted" and requested > 1:
                    requested -= 1
                    continue
                raise
        return self._transport.lookup(code, 1)

    def autocomplete(self, query: str) -> dict[str, Any]:
        return self._transport.autocomplete(query)

    def reverse(self, lng: float, lat: float, max_distance_m: float = 25) -> dict[str, Any]:
        return self._transport.reverse(lng, lat, max_distance_m)

    def nearby(self, lng: float, lat: float, radius: float = 300) -> dict[str, Any]:
        return self._transport.nearby(lng, lat, radius)

    def states(self) -> list[dict[str, str]]:
        return self._transport.states()

    def lgas(self, state: str) -> list[dict[str, str]]:
        return self._transport.lgas(state)


def build_client(cfg: Settings | None = None) -> PostcodeClient:
    cfg = cfg or default_settings
    if cfg.live:
        return PostcodeClient(LiveTransport(cfg.postcode_api_key, cfg.postcode_api_base), cfg.max_level)
    return PostcodeClient(SimulatedTransport(max_level=cfg.max_level), cfg.max_level)
