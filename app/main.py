"""GuardPoint HTTP service.

Endpoints fall into three groups:

* ``/api/verify`` — the core product: score one client against the postcode
  registry and return an explainable verdict.
* ``/api/portfolio`` — the whole book, summarised, for a dashboard.
* ``/api/postcode/*`` — thin, typed pass-throughs to the NIPOST gateway so an
  operator can explore the registry directly.
* ``/api/audit`` — the immutable record of what was checked, when and by whom.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import BasicAuthMiddleware
from .clients import ClientBook
from .config import settings
from .db import Store
from .postcode_client import PostcodeError, build_client
from .postcodes import Segments, parse
from .risk_engine import RiskEngine

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="GuardPoint",
    version="1.0.0",
    description="Postcode-anchored risk & compliance verification for Nigerian businesses.",
)

# Only when DASHBOARD_USER and DASHBOARD_PASSWORD are both set; otherwise
# the app is open, which keeps local use and the simulator frictionless.
if settings.auth_enabled:
    app.add_middleware(BasicAuthMiddleware)

pc_client = build_client()
store = Store(settings.db_path)
book = ClientBook()
engine = RiskEngine(pc_client, settings)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class ClientInput(BaseModel):
    id: str = Field(default="ADHOC", description="Client reference; use ADHOC for one-off checks.")
    name: str
    sector: str = "unknown"
    declared_address: str | None = None
    postcode: str | None = None
    declared_coordinates: list[float] | None = None
    registration_status: str | None = "active"
    rc_number: str | None = None
    tax_id: str | None = None
    incorporation_year: int | None = None
    business_description: str | None = None
    directors: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": settings.mode,
        "transport": pc_client.transport_name,
        "max_level": pc_client.max_level,
        "risk_thresholds": {
            "review": settings.risk_review_threshold,
            "block": settings.risk_block_threshold,
        },
    }


@app.get("/api/policy", tags=["meta"])
def get_policy() -> dict[str, Any]:
    """Expose the active policy so scores can be interpreted by a UI."""
    from . import policy

    return {
        "sector_tiers": policy.SECTOR_TIERS,
        "sector_weights": policy.SECTOR_WEIGHTS,
        "thresholds": {
            "review": settings.risk_review_threshold,
            "block": settings.risk_block_threshold,
        },
    }


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


@app.get("/api/clients", tags=["portfolio"])
def list_clients() -> dict[str, Any]:
    clients = book.all()
    out = []
    for c in clients:
        latest = store.latest_for_client(c["id"])
        out.append(
            {
                "id": c["id"],
                "name": c["name"],
                "sector": c["sector"],
                "postcode": c.get("postcode"),
                "peers_at_address": book.peers_at(c.get("postcode")),
                "last_verification": (
                    {
                        "score": latest["score"],
                        "tier": latest["tier"],
                        "decision": latest["decision"],
                        "at": latest["created_at"],
                    }
                    if latest
                    else None
                ),
            }
        )
    return {"clients": out}


@app.get("/api/clients/{client_id}", tags=["portfolio"])
def get_client(client_id: str) -> dict[str, Any]:
    client = book.get(client_id)
    if client is None:
        raise HTTPException(status_code=404, detail=f"unknown client '{client_id}'")
    return {"client": client, "peers_at_address": book.peers_at(client.get("postcode"))}


@app.post("/api/verify", tags=["verification"])
def verify(client: ClientInput) -> dict[str, Any]:
    """Score one client and persist the assessment."""
    data = client.model_dump()
    peers = book.peers_at(data.get("postcode")) if data["id"] != "ADHOC" else 1
    assessment = engine.assess(data, peer_count=peers)
    result = assessment.as_dict()
    result["explanation"] = assessment.explain()

    record_id = store.save_verification(result, mode=settings.mode)
    store.audit(
        actor="api",
        action="verify_client",
        subject=data.get("id"),
        detail={
            "score": result["score"],
            "decision": result["decision"],
            "postcode": result.get("postcode"),
        },
    )
    result["record_id"] = record_id
    return result


@app.get("/api/verify/{client_id}", tags=["verification"])
def verify_known_client(client_id: str, persist: bool = True) -> dict[str, Any]:
    """Verify a client already on the book."""
    client = book.get(client_id)
    if client is None:
        raise HTTPException(status_code=404, detail=f"unknown client '{client_id}'")
    assessment = engine.assess(client, peer_count=book.peers_at(client.get("postcode")))
    result = assessment.as_dict()
    result["explanation"] = assessment.explain()
    if persist:
        record_id = store.save_verification(result, mode=settings.mode)
        store.audit(
            actor="api",
            action="verify_client",
            subject=client_id,
            detail={"score": result["score"], "decision": result["decision"]},
        )
        result["record_id"] = record_id
    return result


@app.post("/api/portfolio/screen", tags=["portfolio"])
def screen_portfolio(persist: bool = True) -> dict[str, Any]:
    """Verify every client on the book and return a ranked summary."""
    rows = []
    counts = {"approve": 0, "review": 0, "block": 0}
    for client in book.all():
        assessment = engine.assess(client, peer_count=book.peers_at(client.get("postcode")))
        result = assessment.as_dict()
        result["explanation"] = assessment.explain()
        counts[result["decision"]] = counts.get(result["decision"], 0) + 1
        if persist:
            store.save_verification(result, mode=settings.mode)
        rows.append(result)
    rows.sort(key=lambda r: r["score"], reverse=True)
    if persist:
        store.audit(
            actor="api",
            action="screen_portfolio",
            detail={"count": len(rows), "decisions": counts},
        )
    return {
        "count": len(rows),
        "decisions": counts,
        "mode": settings.mode,
        "results": rows,
    }


@app.get("/api/audit", tags=["audit"])
def audit_trail(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"entries": store.audit_entries(limit)}


@app.get("/api/records", tags=["audit"])
def verification_records(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return {"records": store.recent_verifications(limit)}


# --------------------------------------------------------------------------- #
# Postcode gateway pass-through
# --------------------------------------------------------------------------- #


def _guard(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except PostcodeError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_dict()) from exc


@app.get("/api/postcode/lookup", tags=["postcode"])
def postcode_lookup(code: str, level: int = Query(1, ge=1, le=5)) -> dict[str, Any]:
    result = _guard(pc_client.lookup, code, level)
    return {
        "postcode": result.postcode,
        "valid": result.valid,
        "level_served": result.level_served,
        "administrative_address": result.administrative_address,
        "recent_house_address": result.recent_house_address,
        "building_use_status": result.building_use_status,
        "other_building_info": result.other_building_info,
        "point_geometry": result.point_geometry,
    }


@app.get("/api/postcode/autocomplete", tags=["postcode"])
def postcode_autocomplete(q: str) -> dict[str, Any]:
    return _guard(pc_client.autocomplete, q)


@app.get("/api/postcode/reverse", tags=["postcode"])
def postcode_reverse(
    lng: float, lat: float, max_distance_m: float = Query(25, ge=0, le=250)
) -> dict[str, Any]:
    return _guard(pc_client.reverse, lng, lat, max_distance_m)


@app.get("/api/postcode/nearby", tags=["postcode"])
def postcode_nearby(lng: float, lat: float, radius: float = Query(300, ge=0)) -> dict[str, Any]:
    return _guard(pc_client.nearby, lng, lat, radius)


@app.get("/api/postcode/disassemble", tags=["postcode"])
def postcode_disassemble(code: str) -> dict[str, Any]:
    try:
        seg: Segments = parse(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"code": "invalid_postcode",
                                                              "message": str(exc)}}) from exc
    return {"data": seg.as_dict(), "display": seg.display(), "compact": seg.compact()}


@app.get("/api/postcode/states", tags=["postcode"])
def postcode_states() -> dict[str, Any]:
    return {"states": _guard(pc_client.states)}


@app.get("/api/postcode/lgas", tags=["postcode"])
def postcode_lgas(state: str) -> dict[str, Any]:
    return {"state": state.upper(), "lgas": _guard(pc_client.lgas, state)}
