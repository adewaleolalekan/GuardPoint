"""Risk policy: the tunable, auditable data behind every score.

This module holds *policy*, not logic. A compliance officer can edit the sector
tiers, screening terms and signal weights here without touching the engine, and
every change is reviewable in git. That separation is deliberate: an
explainable score needs its inputs to be inspectable.

Screening lists are illustrative stand-ins. In production they are replaced by a
real screening feed (sanctions, PEP, adverse media) — see README.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Sector inherent risk
# --------------------------------------------------------------------------- #
# Higher-risk sectors carry more inherent money-laundering / fraud exposure and
# therefore need stronger controls before onboarding.

SECTOR_TIERS: dict[str, str] = {
    # High inherent risk
    "cryptocurrency": "high",
    "crypto_exchange": "high",
    "money_services": "high",
    "bureau_de_change": "high",
    "money_lending": "high",
    "gambling": "high",
    "betting": "high",
    "arms_dealing": "high",
    "precious_metals": "high",
    "pawnbroking": "high",
    "oil_and_gas_trading": "high",
    "real_estate": "high",
    # Medium inherent risk
    "financial_services": "medium",
    "insurance": "medium",
    "logistics": "medium",
    "import_export": "medium",
    "construction": "medium",
    "hospitality": "medium",
    "travel_agency": "medium",
    "waste_management": "medium",
    "pharmaceutical": "medium",
    "automotive_dealing": "medium",
    "telecoms": "medium",
    "agriculture_export": "medium",
    # Low inherent risk
    "software": "low",
    "professional_services": "low",
    "education": "low",
    "healthcare": "low",
    "retail": "low",
    "manufacturing": "low",
    "agriculture": "low",
    "media": "low",
    "consulting": "low",
}

SECTOR_WEIGHTS: dict[str, int] = {"low": 0, "medium": 10, "high": 20, "unknown": 8}

# Building-use values returned by lookup level 3, and which declared sectors are
# credible for each. A financial-services firm registered at a purely
# residential unit is a classic shell-company tell.
BUILDING_USE_ALLOWED_SECTORS: dict[str, set[str]] = {
    "residential": {"retail", "professional_services", "software", "consulting", "education"},
    "commercial": set(SECTOR_TIERS),  # anything may operate from commercial premises
    "mixed": set(SECTOR_TIERS),
    "industrial": {
        "manufacturing",
        "logistics",
        "import_export",
        "oil_and_gas_trading",
        "waste_management",
        "construction",
        "automotive_dealing",
        "agriculture_export",
    },
}

# Sectors that must never be the sole occupant of a residential unit.
SENSITIVE_SECTORS = {
    "cryptocurrency",
    "crypto_exchange",
    "money_services",
    "bureau_de_change",
    "money_lending",
    "gambling",
    "betting",
    "arms_dealing",
    "precious_metals",
    "financial_services",
    "insurance",
}


# --------------------------------------------------------------------------- #
# Name screening (illustrative)
# --------------------------------------------------------------------------- #
# These are deliberately fictional sample terms used to exercise the engine.
# A real deployment swaps this for a screening provider's API.

SANCTIONS_TERMS: set[str] = {
    "sanctioned holdings",
    "sdn trading",
    "prohibited exports ltd",
}

PEP_TERMS: set[str] = {
    "senatorial",
    "commissioner for",
    "special adviser",
    "board of trustees political",
}

ADVERSE_MEDIA_TERMS: set[str] = {
    "fraud",
    "money laundering",
    "shell company",
    "ponzi",
    "bribery",
    "tax evasion",
    "insolvency",
    "winding up petition",
    "court judgement",
}

# Terms in a declared business description that warrant a closer look.
HIGH_RISK_ACTIVITY_TERMS: set[str] = {
    "unlicensed",
    "offshore transfer",
    "cash intensive",
    "peer-to-peer exchange",
    "gift card",
    "prepaid card",
    "remittance corridor",
    "hawala",
    "virtual asset",
}


# --------------------------------------------------------------------------- #
# Signal weights
# --------------------------------------------------------------------------- #
# Address integrity (derived from the postcode API)
W_UNPARSEABLE = 25
W_NOT_FOUND = 30
W_ADDRESS_MISMATCH = 25
W_SENSITIVE_AT_RESIDENTIAL = 15
W_SECTOR_USE_MISMATCH = 10
W_GEO_MISMATCH = 35
W_GEO_UNCONFIRMED = 12
W_STATE_MISMATCH = 20
W_SHARED_UNIT = 15

# Documentation / entity
W_NO_REGISTRATION = 15
W_REGISTRATION_INACTIVE = 20
W_NO_TAX_ID = 10
W_DIRECTOR_ID_MISSING = 10

# Screening
W_SANCTIONS = 50
W_WATCHLIST = 30
W_PEP = 20
W_ADVERSE_MEDIA = 15
W_HIGH_RISK_ACTIVITY = 15

# A cluster of unrelated entities sharing one building unit is a shell-farm tell.
SHARED_UNIT_TRIGGER = 3

# Signals that force a block regardless of the numeric score. A sanctions match
# must never be routed to "review": no amount of otherwise-clean data outweighs
# it, and a score-based rule would let a well-documented sanctioned entity slip
# through below the threshold.
HARD_BLOCK_SIGNALS: set[str] = {"sanctions_hit"}
