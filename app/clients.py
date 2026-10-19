"""Loads the sample client book and exposes co-location counts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent / "data" / "clients.json"


class ClientBook:
    def __init__(self, path: Path | str = DATA_FILE) -> None:
        raw = json.loads(Path(path).read_text())
        self._clients: list[dict[str, Any]] = raw["clients"]
        self._by_id = {c["id"]: c for c in self._clients}
        self._postcode_counts = Counter(c.get("postcode") for c in self._clients)

    def all(self) -> list[dict[str, Any]]:
        return list(self._clients)

    def get(self, client_id: str) -> dict[str, Any] | None:
        return self._by_id.get(client_id)

    def peers_at(self, postcode: str | None) -> int:
        """How many distinct client entities share this postcode."""
        if not postcode:
            return 1
        return self._postcode_counts.get(postcode, 1)
