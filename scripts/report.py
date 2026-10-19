#!/usr/bin/env python3
"""Print a compliance report for the client book.

    python scripts/report.py                # full portfolio
    python scripts/report.py CLI-0004       # one client, with full evidence

Runs against the simulator by default; set POSTCODE_API_KEY to use the live
gateway. This is the artefact an analyst hands to a file — every number traces
to a named signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients import ClientBook  # noqa: E402
from app.config import settings  # noqa: E402
from app.postcode_client import build_client  # noqa: E402
from app.risk_engine import RiskEngine  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
RED, YEL, GRN = "\033[31m", "\033[33m", "\033[32m"
COLOUR = {"block": RED, "review": YEL, "approve": GRN}


def print_portfolio(engine: RiskEngine, book: ClientBook) -> None:
    rows = [
        engine.assess(c, peer_count=book.peers_at(c.get("postcode"))) for c in book.all()
    ]
    rows.sort(key=lambda a: a.score, reverse=True)

    print(f"\n{BOLD}GuardPoint portfolio report{RESET}  "
          f"{DIM}[{settings.mode} registry · review≥{settings.risk_review_threshold} "
          f"block≥{settings.risk_block_threshold}]{RESET}\n")
    print(f"{'ID':9} {'CLIENT':34} {'SCORE':>5}  {'DECISION':9} TOP SIGNAL")
    print("-" * 100)
    for a in rows:
        colour = COLOUR[a.decision]
        top = max(a.signals, key=lambda s: s.weight, default=None)
        top_label = top.label if top else "—"
        print(
            f"{a.client_id:9} {a.client_name[:34]:34} {a.score:>5}  "
            f"{colour}{a.decision:9}{RESET} {top_label}"
        )

    counts: dict[str, int] = {}
    for a in rows:
        counts[a.decision] = counts.get(a.decision, 0) + 1
    print("-" * 100)
    print(
        f"{len(rows)} clients · "
        f"{GRN}{counts.get('approve', 0)} approved{RESET} · "
        f"{YEL}{counts.get('review', 0)} to review{RESET} · "
        f"{RED}{counts.get('block', 0)} blocked{RESET}\n"
    )


def print_client(engine: RiskEngine, book: ClientBook, client_id: str) -> None:
    client = book.get(client_id)
    if client is None:
        print(f"unknown client '{client_id}'", file=sys.stderr)
        raise SystemExit(1)
    a = engine.assess(client, peer_count=book.peers_at(client.get("postcode")))
    colour = COLOUR[a.decision]

    print(f"\n{BOLD}{a.client_name}{RESET}  ({a.client_id})")
    print(f"Sector: {a.sector} · Postcode: {a.postcode or '—'}")
    print(f"Score: {BOLD}{a.score}/100{RESET} · Tier: {a.tier} · "
          f"Decision: {colour}{BOLD}{a.decision.upper()}{RESET}\n")

    print(f"{BOLD}Signals{RESET}")
    if not a.signals:
        print("  none")
    for s in a.signals:
        print(f"  +{s.weight:<3} [{s.category:13}] {s.label}")
        print(f"       {DIM}{s.evidence}{RESET}")

    print(f"\n{BOLD}Checks performed{RESET}")
    for c in a.checks:
        mark = f"{GRN}✓{RESET}" if c.passed else f"{RED}✗{RESET}"
        print(f"  {mark} {c.name:42} {DIM}{c.detail}{RESET}")

    print(f"\n{BOLD}Required actions{RESET}")
    for action in a.required_actions:
        print(f"  • {action}")
    print()


def main() -> None:
    engine = RiskEngine(build_client(), settings)
    book = ClientBook()
    if len(sys.argv) > 1:
        for client_id in sys.argv[1:]:
            print_client(engine, book, client_id)
    else:
        print_portfolio(engine, book)


if __name__ == "__main__":
    main()
