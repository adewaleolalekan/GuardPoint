# AGENTS.md

Repository notes for future sessions.

## What this is

**GuardPoint** — postcode-anchored risk & compliance verification for Nigerian
businesses, built on the NIPOST Postcode API. It cross-checks a client's
declared address against the national postcode registry and returns an
explainable risk verdict.

## Layout

- `app/config.py` — env-driven settings; `settings.live` decides transport.
- `app/postcodes.py` — parse/normalise/validate the 5-segment postcode.
- `app/postcode_client.py` — `PostcodeClient` facade over `LiveTransport`
  (real gateway) and `SimulatedTransport` (bundled dataset). `build_client()`
  picks one. Keep both transports behaviourally identical.
- `app/policy.py` — **policy data only**: sector tiers, screening lists, signal
  weights, thresholds, `HARD_BLOCK_SIGNALS`.
- `app/risk_engine.py` — the logic. Keep policy out of here.
- `app/clients.py` + `app/data/clients.json` — the sample client book.
- `app/db.py` — SQLite; audit stores hashed identifiers, never raw.
- `app/main.py` — FastAPI service; `app/static/index.html` — dashboard.
- `scripts/report.py` — CLI compliance report.

## Commands

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                      # 39 tests
uvicorn app.main:app --reload                   # dashboard at :8000
python scripts/report.py [CLIENT_ID]            # CLI report
```

No API key is needed: the app runs against the simulator. Set
`POSTCODE_API_KEY` (see `.env.example`) to hit the live gateway.

## Conventions and invariants

- **Policy lives in `policy.py`; logic lives in `risk_engine.py`.** When adding
  a signal, add its weight to `policy.py` and its operator action to the
  `action_map` in `risk_engine._actions`. A test asserts every signal has a
  mapped action.
- **A sanctions hit is a hard block, not a score.** It is in
  `policy.HARD_BLOCK_SIGNALS` and bypasses thresholds. Do not make it
  score-based — a well-documented sanctioned entity would score below threshold.
- **Never request a lookup level above `settings.max_level`.** Use
  `PostcodeClient.lookup_graded`, which degrades gracefully when a level is
  refused.
- **L1 and search are free; L2+ spend credits.** The simulator meters credits the
  same way; keep that in sync so books can be cost-priced before going live.
- Postcode segment rules: state 2 alpha, LGA 2 numeric, district 3 alphanumeric,
  area 2 alpha, unit 2 numeric. Area must be alpha — a digit there is a bug
  (this bit us in seed data once).
- The seed `clients.json` must keep every postcode well-formed; a test enforces
  it. Declared coordinates must also be consistent with the postcode or the geo
  check will (correctly) flag them.

## Git workflow — keep local and GitHub in lockstep

**The GitHub repo `adewaleolalekan/GuardPoint` is the source of truth, and the
local checkout must always match it.** The user works from GitHub.

Before starting work:

```bash
git fetch origin && git status          # confirm clean and not behind
```

After **any** local change, land it on `main` in the same session — do not leave
work sitting uncommitted or unpushed:

```bash
git add -A && git commit -m "<what and why>" && git push origin main
```

Rules:

- **Push to `main` directly.** No feature branches, no pull requests, unless the
  user explicitly asks otherwise. (This is a deliberate, standing exception to
  the usual "never push to main" default.)
- Commit before ending a task. A change that exists only on disk is not
  delivered — the user reads GitHub, not this sandbox.
- Never leave the tree dirty between sessions.
- `/workspace/project` and `/workspace/GuardPoint` are **two clones of the same
  repo**. Keep both on `main`; if they drift, `git fetch origin && git reset
  --hard origin/main`. `/workspace/project` is the one with the working
  environment (deps installed), so prefer editing there.
- Verify a push landed: `git rev-parse HEAD` must equal
  `git rev-parse origin/main`.

## Access control posture

`app/data/clients.json` is **synthetic fixture data** — invented names, RC
numbers and TINs. Do not treat it as a real client book, and do not describe
its exposure as a data-protection incident. That mistake once led to
over-escalating a Cloudflare Access setup the user did not need.

Auth is optional and off by default: `DASHBOARD_USER` + `DASHBOARD_PASSWORD`
both set enables HTTP Basic on every route including `/docs`. It is a
single-operator stopgap, not a user system — no sessions, no per-user audit,
no individual revocation. Real client data would need proper application
auth, which does not exist yet.

## API notes (external)

- Docs: https://docs.postcode.gov.ng/ ; index at `/llms.txt`, pages as `.md`.
- Every endpoint needs `X-API-Key`. `nipost_test_…` is free but reads a sandbox
  of public places only; `nipost_live_…` needs KYB and is metered.
- Error envelope: `{"error": {"code": ..., "message": ...}}`. Statuses 401/402/
  403/429.
- Reverse geocode default 25 m, hard ceiling 250 m (silently clamped).
