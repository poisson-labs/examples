"""The frozen source tables the generator reads, with their digests checked before use.

Every file under ``sources/`` carries its URL, the date it was read and a digest in
``manifest.json``. The generator refuses to run against a file whose digest differs.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clock import DAY, OPERATOR_OFFSET, WINDOW_END, WINDOW_START, Calendar, local_date

DEFAULT_SOURCES = Path(__file__).resolve().parent / "sources"

CORRIDOR_CURRENCY: dict[str, str] = {
    "MX": "MXN",
    "BR": "BRL",
    "PH": "PHP",
    "DE": "EUR",
    "US": "USD",
}
FIXING_FILES: dict[str, str] = {
    "MX": "fixings/usd_mxn_banxico_fix.csv",
    "BR": "fixings/usd_brl_bcb_ptax.csv",
    "PH": "fixings/usd_php_bsp_reference.csv",
    "DE": "fixings/usd_eur_ecb_reference.csv",
}
FIXING_NAMES: dict[str, str] = {
    "MX": "Banco de México FIX",
    "BR": "Banco Central do Brasil PTAX",
    "PH": "Bangko Sentral ng Pilipinas reference rate",
    "DE": "ECB euro reference rate",
}


class SourceDigestMismatch(RuntimeError):
    """A frozen source file no longer matches the digest in the manifest."""


CURRENCY_CORRIDOR: dict[str, str] = {v: k for k, v in CORRIDOR_CURRENCY.items()}


def probe_corridor(book: str) -> str | None:
    """The corridor a saved order-book probe quotes: the book's quote currency is the last three
    letters of its name (``usdt_mxn``, ``USDC-BRL``, ``USDCEUR``)."""
    return CURRENCY_CORRIDOR.get(book[-3:].upper())


@dataclass(frozen=True)
class MarkupFloor:
    """The lower end of one corridor's markup band: the world's band end, raised to the widest
    quoted spread saved for that corridor so that no modelled provider quotes tighter than the
    exchange did on the probe day."""

    corridor: str
    floor_pct: float  # the band's lower end for this corridor, in percent
    market_spread_pct: float | None  # the widest saved quoted spread for the corridor, or None
    venue: str | None
    book: str | None

    @property
    def source(self) -> str:
        """Where the floor comes from, naming the saved book and never the venue: the venues
        probed are public exchanges, and one of them is also a name on the exclusion list."""
        if self.market_spread_pct is None:
            return "the world's band"
        return f"the saved {self.book} order-book snapshot, quoted spread {self.market_spread_pct}%"


def markup_floors(
    sanity: dict[str, Any], band_pct: tuple[float, float], excluded_pct: frozenset[float]
) -> dict[str, MarkupFloor]:
    """Per corridor, the floor of the markup band: the larger of the band's lower end and the
    widest quoted top-of-book spread saved for that corridor; a floor that would sit within
    0.005 points of an excluded figure (in percent, two decimals) is raised by 0.005 until it
    does not. The floor never exceeds the band's upper end."""
    lo, hi = band_pct
    widest: dict[str, dict[str, Any]] = {}
    for probe in sanity["probes"]:
        corridor = probe_corridor(str(probe["book"]))
        if corridor is None:
            continue
        quoted = float(probe["quoted_spread_pct"])
        if corridor not in widest or quoted > float(widest[corridor]["quoted_spread_pct"]):
            widest[corridor] = probe
    out: dict[str, MarkupFloor] = {}
    for corridor in sorted(CORRIDOR_CURRENCY):
        probe = widest.get(corridor)
        spread: float | None = float(probe["quoted_spread_pct"]) if probe is not None else None
        floor: float = lo if spread is None else max(lo, spread)
        for _ in range(100):
            if all(abs(round(floor, 2) - ex) >= 0.005 for ex in excluded_pct):
                break
            floor = round(floor + 0.005, 4)
        floor = min(round(floor, 4), hi)
        out[corridor] = MarkupFloor(
            corridor=corridor,
            floor_pct=floor,
            market_spread_pct=spread,
            venue=str(probe["venue"]) if probe is not None else None,
            book=str(probe["book"]) if probe is not None else None,
        )
    return out


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class FixingRow:
    date: dt.date
    rate: float  # destination currency per USD
    published: str  # the value as published
    banking_day: bool  # false where carried forward
    source_date: dt.date  # the banking day the value comes from


@dataclass
class Sources:
    root: Path
    manifest: dict[str, str]  # path -> sha256
    calendars: dict[str, Calendar]
    calendar_docs: dict[str, dict[str, Any]]
    fixings: dict[str, list[FixingRow]]  # corridor -> one row per calendar day of the window
    fixing_meta: dict[str, dict[str, Any]]
    fees: dict[str, Any]
    rails: dict[str, Any]
    excluded: dict[str, Any]
    sanity: dict[str, Any]
    excluded_figures: frozenset[float] = field(default_factory=frozenset)

    def digest_of(self, rel: str) -> str:
        return self.manifest[rel]

    def fixing_on(self, corridor: str, t: int) -> FixingRow:
        d = local_date(t, OPERATOR_OFFSET)
        rows = self.fixings[corridor]
        idx = (d - rows[0].date).days
        if idx < 0:
            return rows[0]
        if idx >= len(rows):
            return rows[-1]
        return rows[idx]


def _check_manifest(root: Path) -> dict[str, str]:
    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    digests: dict[str, str] = {}
    for entry in man["files"]:
        p = root / entry["path"]
        if not p.exists():
            raise SourceDigestMismatch(f"missing frozen source {entry['path']}")
        actual = sha256_file(p)
        if actual != entry["sha256"]:
            raise SourceDigestMismatch(
                f"frozen source {entry['path']} has digest {actual[:12]}, manifest says {entry['sha256'][:12]}"
            )
        digests[entry["path"]] = entry["sha256"]
    return digests


def _load_fixing(path: Path, invert: bool) -> list[FixingRow]:
    with path.open(encoding="utf-8") as f:
        raw = [(dt.date.fromisoformat(r["date"]), r["rate"]) for r in csv.DictReader(f)]
    raw.sort()
    by_date = {d: v for d, v in raw}
    start = local_date(WINDOW_START, OPERATOR_OFFSET)
    end = local_date(WINDOW_END, OPERATOR_OFFSET)
    out: list[FixingRow] = []
    last: tuple[dt.date, str] | None = None
    d = start
    while d <= end:
        if d in by_date:
            last = (d, by_date[d])
            banking = True
        else:
            banking = False
        if last is None:
            raise ValueError(f"no fixing on or before {d} in {path.name}")
        rate = float(last[1])
        if invert:
            rate = 1.0 / rate
        out.append(
            FixingRow(
                date=d, rate=rate, published=last[1], banking_day=banking, source_date=last[0]
            )
        )
        d = d + dt.timedelta(days=1)
    assert len(out) == (WINDOW_END - WINDOW_START) // DAY + 1
    return out


def load_sources(root: Path | None = None) -> Sources:
    root = Path(root) if root is not None else DEFAULT_SOURCES
    manifest = _check_manifest(root)
    calendars: dict[str, Calendar] = {}
    calendar_docs: dict[str, dict[str, Any]] = {}
    for dest in ("US", "MX", "BR", "PH", "DE"):
        doc = json.loads((root / "calendars" / f"{dest.lower()}.json").read_text(encoding="utf-8"))
        days = [dt.date.fromisoformat(x["date"]) for x in doc["non_banking_weekdays_in_window"]]
        calendars[dest] = Calendar(dest, days)
        calendar_docs[dest] = doc
    fixings: dict[str, list[FixingRow]] = {}
    fixing_meta: dict[str, dict[str, Any]] = {}
    for corridor, rel in FIXING_FILES.items():
        fixings[corridor] = _load_fixing(root / rel, invert=(corridor == "DE"))
        fixing_meta[corridor] = json.loads(
            (root / rel.replace(".csv", ".meta.json")).read_text(encoding="utf-8")
        )
    fees = json.loads((root / "fees.json").read_text(encoding="utf-8"))
    rails = json.loads((root / "rails.json").read_text(encoding="utf-8"))
    excluded = json.loads((root / "excluded_figures.json").read_text(encoding="utf-8"))
    sanity = json.loads((root / "fx_sanity.json").read_text(encoding="utf-8"))
    figures = {round(float(v), 2) for v in excluded["figures_pct"]}
    return Sources(
        root=root,
        manifest=manifest,
        calendars=calendars,
        calendar_docs=calendar_docs,
        fixings=fixings,
        fixing_meta=fixing_meta,
        fees=fees,
        rails=rails,
        excluded=excluded,
        sanity=sanity,
        excluded_figures=frozenset(figures),
    )
