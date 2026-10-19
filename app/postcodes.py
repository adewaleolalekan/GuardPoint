"""Parsing and normalisation for Nigerian postcodes.

A postcode has five segments::

    AA 99 H77 BB 55
    |  |  |   |  +-- building unit within the area  (2 numeric)
    |  |  |   +----- area within the district       (2 alpha)
    |  |  +--------- district within the LGA        (3 alphanumeric)
    |  +------------ LGA within the state           (2 numeric)
    +--------------- state                          (2 alpha)

The gateway is documented as tolerant of spaces, hyphens and case, so every
function here accepts all three styles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEGMENT_RE = re.compile(
    r"^(?P<state>[A-Za-z]{2})[- ]?"
    r"(?P<lga>\d{2})[- ]?"
    r"(?P<district>[A-Za-z0-9]{3})[- ]?"
    r"(?P<area>[A-Za-z]{2})[- ]?"
    r"(?P<unit>\d{2})$"
)


@dataclass(frozen=True)
class Segments:
    state: str
    lga: str
    district: str
    area: str
    unit: str

    def compact(self) -> str:
        return f"{self.state}{self.lga}{self.district}{self.area}{self.unit}"

    def dashed(self) -> str:
        return f"{self.state}-{self.lga}-{self.district}-{self.area}-{self.unit}"

    def display(self) -> str:
        return f"{self.state} {self.lga} {self.district} {self.area} {self.unit}"

    def as_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "lga": self.lga,
            "district": self.district,
            "area": self.area,
            "unit": self.unit,
        }


def normalise(code: str) -> str:
    """Collapse any accepted style to the canonical dashed form.

    Raises ValueError when the code cannot be read as five segments.
    """
    return parse(code).dashed()


def parse(code: str) -> Segments:
    """Split a postcode into its five segments, tolerating spaces/hyphens/case."""
    if code is None:
        raise ValueError("postcode is required")
    cleaned = code.strip()
    if not cleaned:
        raise ValueError("postcode is required")
    match = _SEGMENT_RE.match(cleaned)
    if not match:
        raise ValueError(f"'{code}' is not a well-formed Nigerian postcode")
    g = match.groupdict()
    return Segments(
        state=g["state"].upper(),
        lga=g["lga"],
        district=g["district"].upper(),
        area=g["area"].upper(),
        unit=g["unit"],
    )


def is_valid(code: str) -> bool:
    try:
        parse(code)
    except ValueError:
        return False
    return True


def depth(code: str) -> int:
    """How many segments a partial code fills (0-5). Used by autocomplete."""
    cleaned = re.sub(r"[\s-]", "", code or "").upper()
    if not cleaned:
        return 0
    widths = (2, 2, 3, 2, 2)
    consumed = 0
    filled = 0
    for width in widths:
        if consumed >= len(cleaned):
            break
        consumed += width
        filled += 1
    return filled
