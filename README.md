# GuardPoint

**Postcode-anchored risk & compliance for Nigerian businesses.**

GuardPoint is a working product built on the [NIPOST Postcode API](https://docs.postcode.gov.ng/).
It answers the question a Nigerian consultancy gets asked constantly: *"is this
business actually where it says it is?"* — and turns the answer into an
explainable risk verdict a compliance officer can defend in an audit.

---

## Why this exists

KYC in Nigeria has a specific failure mode. A prospective client presents a CAC
certificate and an address. The certificate is real. The address may not be.
Shell companies register at residential units, crypto and money-services firms
claim prestigious offices they do not occupy, and the same one-room premises
turns up behind a dozen unrelated "holdings" companies. Verifying this used to
mean a site visit per client.

Nigeria's national postcode system fixes that. Every postcode is a
12-character, five-segment code that resolves to a specific *building unit*:

```
FC 04 A01 MH 01
│  │  │   │  └─ building unit within the area   (2 numeric)
│  │  │   └──── area within the district        (2 alpha)
│  │  └──────── district within the LGA         (3 alphanumeric)
│  └─────────── LGA within the state            (2 numeric)
└────────────── state                           (2 alpha)
```

That granularity — down to the building, with an authoritative record of the
premises' *use* — is what makes address verification a hard, automatable check
rather than a judgement call. GuardPoint is the layer that turns it into a
compliance decision.

---

## What it does

For each client it:

1. **Validates the postcode** structurally, then resolves it in the national
   registry (graded lookup L1–L3).
2. **Cross-checks the declared address** against the registry's own record,
   token by token.
3. **Corroborates the coordinates** by reverse-geocoding them and confirming
   they land on the declared postcode — not a different one across the country.
4. **Flags premises misuse** — a regulated activity (crypto, lending, payments)
   registered at a unit the registry records as residential.
5. **Detects co-location clusters** — unrelated entities sharing one building
   unit, the signature of a shell-company farm.
6. **Screens** the entity name, description and directors against
   sanctions/PEP/adverse-media term lists.
7. **Scores the residual risk**, returns a decision
   (`approve` / `review` / `block`), the exact signals behind it, and the
   required actions.

Every verdict is persisted with a full audit trail. Personal identifiers in the
audit log are hashed, never stored raw.

---

## Quickstart

```bash
pip install -r requirements.txt

# Run immediately against the built-in simulator — no API key, no credits.
uvicorn app.main:app --reload
```

Open <http://localhost:8000> for the operator dashboard, or
<http://localhost:8000/docs> for the OpenAPI explorer.

### Switching to the live gateway

The application runs against a faithful simulator until you give it a key.
To go live, copy `.env.example` to `.env` and set:

```bash
POSTCODE_API_KEY=nipost_test_...     # free, unmetered test key
POSTCODE_MAX_LEVEL=3                 # highest level your org is granted
```

Nothing else changes — the endpoints, request shapes and response handling are
identical. The dashboard shows `Live gateway` instead of `Simulated registry`.

> **Test vs live keys.** A `nipost_test_…` key reads a small sandbox of public
> places only; production postcodes will not resolve under it. A `nipost_live_…`
> key needs verified KYB and is metered — L1 is free, L2+ costs credits. Set
> `POSTCODE_MAX_LEVEL` to your granted level; GuardPoint never requests more,
> and degrades gracefully to a lower level if a higher one is refused.

### CLI report

```bash
python scripts/report.py             # ranked portfolio report
python scripts/report.py CLI-0004    # one client, with full evidence
```

---

## How it uses the postcode API

| Capability | Endpoint | Level | Why |
| - | - | - | - |
| Structural validation | `POST /v1/assembly/assemble` | — | Free; normalises any input style before a paid lookup. |
| Graded lookup | `GET /v1/lookup` | L1–L3 | L1 confirms the code exists; L2 adds administrative names and the registered address; L3 adds the building's **use**, which drives premises-misuse detection. |
| Reverse geocode | `GET /v1/search/reverse` | — | Confirms the declared coordinates resolve to the declared postcode. |
| Location search | `GET /v1/search/nearby` | — | Finds units around a coordinate for site verification. |
| Autocomplete | `GET /v1/search/autocomplete` | — | Segment-aware entry, so operators never type a full code by hand. |
| Reference | `GET /v1/reference/*` | — | States, LGAs, districts and areas for dropdowns. |

**Cost discipline.** L1 and search are free; L2+ spend credits. GuardPoint asks
for the cheapest level that satisfies the active check, and the simulator meters
credits the same way so a book can be costed before you go live. Reverse
geocoding is only called when coordinates are supplied, and is clamped to the
documented 250 m ceiling.

---

## Architecture

```
app/
  config.py           Environment-driven settings; live vs simulated mode
  postcodes.py        Parse / normalise / validate the 5-segment code
  postcode_client.py  One interface, two transports (live gateway | simulator)
  policy.py           Sector tiers, screening lists, signal weights — the tunable policy
  risk_engine.py      Cross-checks the registry and scores the residual risk
  clients.py          The sample client book and co-location counts
  db.py               SQLite persistence + hashed-identifier audit trail
  main.py             FastAPI service and postcode pass-throughs
  static/index.html   Operator dashboard
  data/               Simulated registry slice and sample clients
```

The design keeps **policy** (`policy.py`) separate from **logic**
(`risk_engine.py`). A compliance officer can retune sector tiers, screening terms
or signal weights without touching the engine, and every change is reviewable in
git. An explainable score needs its inputs to be inspectable.

The `Transport` protocol is the seam between the two worlds. `LiveTransport`
calls the real gateway; `SimulatedTransport` serves the documented shapes from a
bundled dataset, faithfully reproducing cumulative lookup levels, the 250 m
reverse-geocode ceiling, the error envelope and credit metering. `build_client()`
picks one based on whether a key is configured. Callers never know which is in
play, which is what makes the product testable without a key.

---

## Risk model

Signals are additive, capped at 100, and each traces to named evidence:

| Category | Example signals |
| - | - |
| **Address** | malformed postcode, not in registry, declared address ≠ registered address, coordinates resolve elsewhere, shared-premises cluster |
| **Documentation** | no CAC registration, inactive registration, no TIN, unverified director identity |
| **Screening** | sanctions hit, PEP exposure, adverse media, high-risk activity described |
| **Sector** | inherent-risk tier, regulated activity at a residential unit |

Thresholds are configurable (`RISK_REVIEW_THRESHOLD`, `RISK_BLOCK_THRESHOLD`).

**One rule is deliberately not score-based.** A sanctions match is a hard block
regardless of the numeric score. A well-documented sanctioned entity would
otherwise score low enough to slip through a threshold. A test enforces this.

---

## API

| Method | Path | Purpose |
| - | - | - |
| `GET` | `/api/health` | Mode, transport, granted level, thresholds |
| `GET` | `/api/policy` | Active sector tiers and weights |
| `GET` | `/api/clients` | The book, with last-verification summary |
| `GET` | `/api/clients/{id}` | One client record |
| `POST` | `/api/verify` | Score an ad-hoc client |
| `GET` | `/api/verify/{id}` | Score a client on the book |
| `POST` | `/api/portfolio/screen` | Score the whole book, ranked |
| `GET` | `/api/audit` | Audit trail |
| `GET` | `/api/records` | Persisted verification records |
| `GET` | `/api/postcode/*` | Lookup, autocomplete, reverse, nearby, disassemble, states, lgas |

Example:

```bash
curl -s -X POST localhost:8000/api/verify -H 'Content-Type: application/json' -d '{
  "id": "ADHOC",
  "name": "Sahel Virtual Asset Exchange Ltd",
  "sector": "cryptocurrency",
  "postcode": "RI-02-I77-AB-30",
  "declared_address": "East-West Road, Rumuokoro, Port Harcourt",
  "declared_coordinates": [6.9886, 4.8291],
  "rc_number": "RC1550233",
  "tax_id": "TIN-41120093-0003",
  "business_description": "Peer-to-peer exchange and virtual asset custody",
  "directors": [{"name": "Emeka Obi", "id_verified": true}]
}' | jq '{score, tier, decision, signals: [.signals[].label]}'
```

---

## Tests

```bash
python -m pytest tests/ -q
```

39 tests cover postcode parsing and rejection of malformed codes, cumulative
lookup levels, credit metering, the level-gate and graceful degradation,
reverse-geocode radius handling, each risk rule, the sanctions hard block,
determinism, co-location counting, and every API endpoint including the 403 and
404 paths.

---

## Production notes

This is a complete, runnable product, but a real deployment needs four things
swapped in:

- **Screening data.** `policy.py` ships illustrative term lists. Replace them
  with a screening provider (sanctions, PEP, adverse media) behind the same
  `_check_screening` interface.
- **Real client records.** `clients.py` reads a sample book. Point it at your
  practice-management system.
- **Auth on the service.** The API is unauthenticated for local use. Put it
  behind your SSO before exposing it, and keep the NIPOST **secret** key
  server-side — never in a browser. Publishable keys are for the embeddable
  widget only.
- **Audit salt.** `hash_identifier` in `db.py` should use a configured salt so
  hashes cannot be brute-forced against a known identifier list.

### Regulatory context

The checks map to Nigeria's AML/CFT regime — customer due diligence and
enhanced due diligence under the Money Laundering (Prevention and Prohibition)
Act 2022 and CBN/SCUML expectations for regulated sectors. GuardPoint produces
the evidence and the decision support; the compliance officer owns the decision.

---

## Where this could go next

- **Widget integration.** The NIPOST widget SDKs (Flutter, Android, iOS, React
  Native) let a client pick their own postcode on a map and hand back a
  `PostcodeSelection`. That removes address-entry friction and its typos from
  onboarding entirely.
- **Portfolio monitoring.** Re-screen the book on a schedule and alert when a
  client's registry record changes — a re-classified building use, or a new
  entity appearing at the same unit.
- **Batch onboarding.** The `/api/portfolio/screen` shape already supports it;
  add async jobs and a webhook for large books.
