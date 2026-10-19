"""The risk & compliance engine.

Takes a client's *declared* attributes and cross-checks them against the
authoritative postcode registry, then scores the residual risk and returns an
auditable, explainable verdict.

Design goals:

* **Explainable** — every point of the score traces to a named signal with the
  evidence that triggered it.
* **Conservative** — missing data raises risk rather than lowering it.
* **Deterministic** — same inputs, same score, so a verdict can be reproduced in
  an audit months later.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import policy
from .config import Settings
from .postcode_client import PostcodeClient, PostcodeError
from .postcodes import parse

# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class Signal:
    """One reason the score moved, with the evidence behind it."""

    id: str
    category: str  # address | documentation | screening | sector
    label: str
    weight: int
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Check:
    """A discrete verification step and its outcome, for the operator UI."""

    name: str
    passed: bool
    detail: str
    source: str  # postcode_api | declared | screening

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Assessment:
    client_id: str
    client_name: str
    sector: str
    postcode: str | None
    score: int
    tier: str  # low | medium | high | critical
    decision: str  # approve | review | block
    signals: list[Signal] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
    registry: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "sector": self.sector,
            "postcode": self.postcode,
            "score": self.score,
            "tier": self.tier,
            "decision": self.decision,
            "signals": [s.as_dict() for s in self.signals],
            "checks": [c.as_dict() for c in self.checks],
            "required_actions": self.required_actions,
            "registry": self.registry,
            "notes": self.notes,
        }

    def explain(self) -> str:
        """A one-paragraph narrative an analyst can paste into a case file."""
        if not self.signals:
            return (
                f"{self.client_name} ({self.client_id}) scored {self.score}/100 "
                f"({self.tier} risk) with no adverse signals. Decision: {self.decision.upper()}."
            )
        parts = "; ".join(f"{s.label} (+{s.weight})" for s in self.signals)
        return (
            f"{self.client_name} ({self.client_id}) scored {self.score}/100 "
            f"({self.tier} risk). Signals: {parts}. Decision: {self.decision.upper()}."
        )


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

_STOPWORDS = {
    "the", "and", "of", "ltd", "limited", "nigeria", "plot", "no", "road", "street",
    "avenue", "way", "close", "crescent", "drive", "lane", "zone", "phase", "km",
    "back", "front", "behind", "market", "express", "old", "new", "gra", "ii", "iii",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) >= 3 and t.lower() not in _STOPWORDS and not t.isdigit()
    }


def _address_overlap(declared: str | None, registered: str | None) -> float:
    """Fraction of the declared address's distinctive tokens found in the registry."""
    declared_tokens = _tokens(declared)
    if not declared_tokens:
        return 0.0
    return len(declared_tokens & _tokens(registered)) / len(declared_tokens)


def _contains_any(text: str | None, terms: set[str]) -> list[str]:
    if not text:
        return []
    lowered = text.lower()
    return sorted(t for t in terms if t in lowered)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class RiskEngine:
    def __init__(self, pc_client: PostcodeClient, cfg: Settings) -> None:
        self.pc = pc_client
        self.cfg = cfg

    def assess(self, client: dict[str, Any], peer_count: int = 1) -> Assessment:
        client_id = client.get("id", "?")
        name = client.get("name", "Unknown")
        sector = (client.get("sector") or "unknown").lower()
        postcode = client.get("postcode")

        assessment = Assessment(
            client_id=client_id,
            client_name=name,
            sector=sector,
            postcode=postcode,
            score=0,
            tier="low",
            decision="approve",
        )

        lookup = None
        # ---- 1. Address integrity against the postcode registry ----------- #
        if not postcode:
            self._add(assessment, "address_missing", "address", "No postcode declared", policy.W_NOT_FOUND,
                      "The client supplied no postcode, so the address cannot be verified.")
            assessment.checks.append(
                Check("Postcode declared", False, "No postcode on file.", "declared")
            )
        else:
            try:
                segments = parse(postcode)
                assessment.checks.append(
                    Check(
                        "Postcode well-formed",
                        True,
                        f"Parsed to {segments.dashed()} (state {segments.state}, LGA {segments.lga}).",
                        "postcode_api",
                    )
                )
            except ValueError as exc:
                self._add(assessment, "postcode_unparseable", "address", "Malformed postcode",
                          policy.W_UNPARSEABLE, str(exc))
                assessment.checks.append(Check("Postcode well-formed", False, str(exc), "postcode_api"))

            try:
                # Ask for L3 (names + building use) but degrade to whatever the
                # key is granted; the served level is recorded on the result.
                lookup = self.pc.lookup_graded(postcode, 3)
            except PostcodeError as exc:
                self._add(assessment, "lookup_failed", "address", "Registry lookup failed",
                          policy.W_NOT_FOUND, f"{exc.code}: {exc.message}")
                assessment.checks.append(
                    Check("Postcode found in registry", False, exc.message, "postcode_api")
                )

        if lookup is not None:
            assessment.registry = {
                "postcode": lookup.postcode,
                "valid": lookup.valid,
                "level_served": lookup.level_served,
                "administrative_address": lookup.administrative_address,
                "recent_house_address": lookup.recent_house_address,
                "building_use_status": lookup.building_use_status,
            }
            if lookup.valid:
                assessment.checks.append(
                    Check(
                        "Postcode found in registry",
                        True,
                        f"Resolved to {lookup.recent_house_address or lookup.postcode} "
                        f"({lookup.locality_name or 'n/a'}, {lookup.lga_name or 'n/a'}).",
                        "postcode_api",
                    )
                )
                self._check_address_consistency(assessment, client, lookup)
                self._check_building_use(assessment, sector, lookup)
            else:
                self._add(
                    assessment, "postcode_not_found", "address", "Postcode not in registry",
                    policy.W_NOT_FOUND,
                    f"{lookup.postcode} is structurally valid but is not an active unit.",
                )
                assessment.checks.append(
                    Check("Postcode found in registry", False,
                          "Structurally valid but not an active building.", "postcode_api")
                )

        # ---- 2. Geographic corroboration via reverse geocode -------------- #
        self._check_geometry(assessment, client)

        # ---- 3. Entity documentation -------------------------------------- #
        self._check_documentation(assessment, client)

        # ---- 4. Screening -------------------------------------------------- #
        self._check_screening(assessment, client)

        # ---- 5. Sector & co-location --------------------------------------- #
        self._check_sector(assessment, sector)
        if peer_count >= policy.SHARED_UNIT_TRIGGER:
            self._add(
                assessment, "shared_unit", "address", "Shared premises cluster",
                policy.W_SHARED_UNIT,
                f"{peer_count} distinct client entities share this building unit.",
            )

        # ---- 6. Roll up ---------------------------------------------------- #
        assessment.score = min(100, sum(s.weight for s in assessment.signals))
        assessment.tier = self._tier(assessment.score)
        assessment.decision = self._decision(assessment)
        assessment.required_actions = self._actions(assessment)
        return assessment

    # -- individual checks --------------------------------------------------- #

    def _check_address_consistency(self, a: Assessment, client: dict[str, Any], lookup) -> None:
        declared = client.get("declared_address")
        overlap = _address_overlap(declared, lookup.recent_house_address)
        if declared and lookup.recent_house_address:
            if overlap < 0.34:
                self._add(
                    a, "address_mismatch", "address", "Declared address does not match registry",
                    policy.W_ADDRESS_MISMATCH,
                    f"Only {overlap:.0%} of the declared address matches the registered "
                    f"address '{lookup.recent_house_address}'.",
                )
                a.checks.append(
                    Check("Declared address matches registry", False,
                          f"Low token overlap ({overlap:.0%}).", "postcode_api")
                )
            else:
                a.checks.append(
                    Check("Declared address matches registry", True,
                          f"{overlap:.0%} token overlap with '{lookup.recent_house_address}'.",
                          "postcode_api")
                )

        # The registry's administrative names should appear in the declared address.
        admin_tokens = (
            _tokens(lookup.locality_name) | _tokens(lookup.lga_name) | _tokens(lookup.state_name)
        )
        if declared and admin_tokens and not (admin_tokens & _tokens(declared)):
            self._add(
                a, "area_mismatch", "address", "Declared location differs from postcode area",
                policy.W_STATE_MISMATCH,
                f"None of the registry's area names ({lookup.locality_name}, "
                f"{lookup.lga_name}, {lookup.state_name}) appear in the declared address.",
            )

    def _check_building_use(self, a: Assessment, sector: str, lookup) -> None:
        use = (lookup.building_use_status or "").lower()
        if not use:
            return
        allowed = policy.BUILDING_USE_ALLOWED_SECTORS.get(use)
        if allowed is None:
            return
        if sector not in allowed:
            if use == "residential" and sector in policy.SENSITIVE_SECTORS:
                self._add(
                    a, "sensitive_at_residential", "sector",
                    "Regulated activity at a residential unit",
                    policy.W_SENSITIVE_AT_RESIDENTIAL,
                    f"A '{sector}' business is registered at a unit the registry records "
                    f"as residential.",
                )
                a.checks.append(
                    Check("Premises suitable for declared activity", False,
                          "Regulated sector at a residential building.", "postcode_api")
                )
            else:
                self._add(
                    a, "sector_use_mismatch", "sector",
                    "Premises use inconsistent with sector",
                    policy.W_SECTOR_USE_MISMATCH,
                    f"A '{sector}' business operates from a '{use}' building.",
                )
                a.checks.append(
                    Check("Premises suitable for declared activity", False,
                          f"Declared '{sector}' vs registered use '{use}'.", "postcode_api")
                )
        else:
            a.checks.append(
                Check("Premises suitable for declared activity", True,
                      f"'{use}' premises are consistent with a '{sector}' business.",
                      "postcode_api")
            )

    def _check_geometry(self, a: Assessment, client: dict[str, Any]) -> None:
        coords = client.get("declared_coordinates")
        postcode = client.get("postcode")
        if not coords or not postcode:
            return
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError, IndexError):
            return
        try:
            resolved = self.pc.reverse(lng, lat, max_distance_m=250)
        except PostcodeError:
            return

        if not resolved.get("found"):
            self._add(
                a, "geo_unconfirmed", "address", "Coordinates could not be corroborated",
                policy.W_GEO_UNCONFIRMED,
                "Reverse geocoding found no registered building within 250 m of the "
                "declared coordinates.",
            )
            a.checks.append(
                Check("Coordinates corroborate postcode", False,
                      "No registered unit within 250 m.", "postcode_api")
            )
            return

        resolved_code = resolved["unit"]["postcode"]
        try:
            same = parse(resolved_code).dashed() == parse(postcode).dashed()
        except ValueError:
            same = False
        if same:
            a.checks.append(
                Check("Coordinates corroborate postcode", True,
                      f"Reverse geocode of the declared coordinates resolved to {resolved_code}.",
                      "postcode_api")
            )
        else:
            self._add(
                a, "geo_mismatch", "address", "Declared coordinates resolve elsewhere",
                policy.W_GEO_MISMATCH,
                f"The declared coordinates reverse-geocode to {resolved_code}, not the "
                f"declared {postcode}.",
            )
            a.checks.append(
                Check("Coordinates corroborate postcode", False,
                      f"Coordinates resolve to {resolved_code}.", "postcode_api")
            )

    def _check_documentation(self, a: Assessment, client: dict[str, Any]) -> None:
        if not client.get("rc_number"):
            self._add(a, "no_registration", "documentation", "No CAC registration number",
                      policy.W_NO_REGISTRATION, "No RC number on file.")
            a.checks.append(Check("CAC registration on file", False, "No RC number.", "declared"))
        else:
            a.checks.append(
                Check("CAC registration on file", True, client["rc_number"], "declared")
            )

        status = (client.get("registration_status") or "").lower()
        if status and status != "active":
            self._add(a, "registration_inactive", "documentation", "Registration not active",
                      policy.W_REGISTRATION_INACTIVE, f"CAC status is '{status}'.")
            a.checks.append(
                Check("Registration active", False, f"CAC status '{status}'.", "declared")
            )
        else:
            a.checks.append(Check("Registration active", True, status or "active", "declared"))

        if not client.get("tax_id"):
            self._add(a, "no_tax_id", "documentation", "No tax identification number",
                      policy.W_NO_TAX_ID, "No TIN on file.")
            a.checks.append(Check("Tax identification on file", False, "No TIN.", "declared"))
        else:
            a.checks.append(Check("Tax identification on file", True, client["tax_id"], "declared"))

        directors = client.get("directors") or []
        unverified = [d.get("name", "?") for d in directors if not d.get("id_verified")]
        if not directors:
            self._add(a, "no_directors", "documentation", "No directors recorded",
                      policy.W_DIRECTOR_ID_MISSING, "No directors on file.")
            a.checks.append(Check("Directors identified", False, "None on file.", "declared"))
        elif unverified:
            self._add(a, "director_id_missing", "documentation", "Unverified director identity",
                      policy.W_DIRECTOR_ID_MISSING,
                      "Unverified: " + ", ".join(unverified))
            a.checks.append(
                Check("Directors identified", False, "Unverified: " + ", ".join(unverified),
                      "declared")
            )
        else:
            a.checks.append(
                Check("Directors identified", True, f"{len(directors)} verified.", "declared")
            )

    def _check_screening(self, a: Assessment, client: dict[str, Any]) -> None:
        haystack = " ".join(
            filter(None, [client.get("name"), client.get("business_description")])
        )
        hits = _contains_any(haystack, policy.SANCTIONS_TERMS)
        if hits:
            self._add(a, "sanctions_hit", "screening", "Sanctions screening hit",
                      policy.W_SANCTIONS, "Matched: " + ", ".join(hits))
        a.checks.append(
            Check("Sanctions screening", not hits,
                  "Clear." if not hits else "Matched: " + ", ".join(hits), "screening")
        )

        pep_hits = _contains_any(haystack, policy.PEP_TERMS)
        director_peps = [d.get("name", "?") for d in (client.get("directors") or []) if d.get("pep")]
        if pep_hits or director_peps:
            detail = []
            if pep_hits:
                detail.append("entity text: " + ", ".join(pep_hits))
            if director_peps:
                detail.append("directors: " + ", ".join(director_peps))
            self._add(a, "pep_exposure", "screening", "Politically exposed person exposure",
                      policy.W_PEP, "; ".join(detail))
            a.checks.append(Check("PEP screening", False, "; ".join(detail), "screening"))
        else:
            a.checks.append(Check("PEP screening", True, "Clear.", "screening"))

        media_hits = _contains_any(client.get("business_description"), policy.ADVERSE_MEDIA_TERMS)
        if media_hits:
            self._add(a, "adverse_media", "screening", "Adverse media indicators",
                      policy.W_ADVERSE_MEDIA, "Terms: " + ", ".join(media_hits))
        a.checks.append(
            Check("Adverse media screening", not media_hits,
                  "Clear." if not media_hits else "Terms: " + ", ".join(media_hits), "screening")
        )

        activity_hits = _contains_any(client.get("business_description"),
                                      policy.HIGH_RISK_ACTIVITY_TERMS)
        if activity_hits:
            self._add(a, "high_risk_activity", "screening", "High-risk activity described",
                      policy.W_HIGH_RISK_ACTIVITY, "Terms: " + ", ".join(activity_hits))

    def _check_sector(self, a: Assessment, sector: str) -> None:
        tier = policy.SECTOR_TIERS.get(sector, "unknown")
        weight = policy.SECTOR_WEIGHTS.get(tier, policy.SECTOR_WEIGHTS["unknown"])
        a.notes.append(f"Sector '{sector}' carries {tier} inherent risk.")
        if weight:
            self._add(a, "sector_inherent_risk", "sector",
                      f"{tier.capitalize()} inherent-risk sector", weight,
                      f"'{sector}' is classed {tier} under the current policy.")

    # -- roll-up helpers ----------------------------------------------------- #

    def _add(self, a: Assessment, sid: str, category: str, label: str, weight: int,
             evidence: str) -> None:
        a.signals.append(Signal(sid, category, label, weight, evidence))

    def _tier(self, score: int) -> str:
        if score >= self.cfg.risk_block_threshold:
            return "critical"
        if score >= self.cfg.risk_review_threshold:
            return "high"
        if score >= self.cfg.risk_review_threshold // 2:
            return "medium"
        return "low"

    def _decision(self, a: Assessment) -> str:
        if any(s.id in policy.HARD_BLOCK_SIGNALS for s in a.signals):
            return "block"
        if a.score >= self.cfg.risk_block_threshold:
            return "block"
        if a.score >= self.cfg.risk_review_threshold:
            return "review"
        return "approve"

    def _actions(self, a: Assessment) -> list[str]:
        action_map = {
            "sanctions_hit": "Do not onboard. Escalate to the MLRO and file a report with the NFIU.",
            "pep_exposure": "Apply enhanced due diligence: establish source of wealth and obtain senior management sign-off.",
            "adverse_media": "Complete an adverse media review and record the findings on the file.",
            "high_risk_activity": "Obtain the specific licence for the described activity, or decline.",
            "sector_inherent_risk": "Apply the sector-specific control set before onboarding.",
            "address_mismatch": "Request documentary proof of the premises (utility bill or tenancy agreement).",
            "area_mismatch": "Confirm the operating address and reconcile it with the postcode.",
            "geo_mismatch": "Reconcile the declared coordinates with the operating address; request a site visit if unresolved.",
            "geo_unconfirmed": "Obtain verifiable coordinates or a geotagged photo of the premises.",
            "postcode_not_found": "Obtain a valid postcode for the operating premises and re-verify.",
            "postcode_unparseable": "Correct the postcode and re-verify.",
            "address_missing": "Collect the operating postcode before proceeding.",
            "sensitive_at_residential": "Conduct physical verification of the premises and evidence the operating address.",
            "sector_use_mismatch": "Confirm the actual use of the premises; escalate if inconsistent.",
            "shared_unit": "Investigate the relationship between the entities sharing the address.",
            "no_registration": "Obtain the CAC certificate and re-verify.",
            "registration_inactive": "Obtain a current CAC status report; do not onboard until active.",
            "no_tax_id": "Obtain the tax identification number.",
            "director_id_missing": "Complete identity verification for every director.",
            "no_directors": "Record and verify the directors and beneficial owners.",
            "lookup_failed": "Retry the registry lookup; treat the address as unverified until it succeeds.",
        }
        actions: list[str] = []
        for signal in a.signals:
            action = action_map.get(signal.id)
            if action and action not in actions:
                actions.append(action)
        if a.decision == "block":
            actions.insert(0, "DECLINE the relationship pending compliance review.")
        elif a.decision == "review":
            actions.insert(0, "Route to a compliance officer for manual review.")
        if not actions:
            actions.append("No action required; retain the verification record.")
        return actions
