"""The six invented providers: coverage, vocabularies, and the parameters drawn once per seed.

A synthetic back-office payments world with invented providers; no real company's data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

UNIFIED_STATUSES = ("created", "submitted", "processing", "settled", "failed", "cancelled")
TERMINAL = frozenset({"settled", "failed", "cancelled"})
UNIFIED_REASONS = (
    "invalid_account",
    "account_closed",
    "beneficiary_name_mismatch",
    "limit_exceeded",
    "compliance_hold",
    "provider_rejected",
    "rail_unavailable",
    "insufficient_float",
    "duplicate_submission",
    "cancelled_by_operator",
    "cancelled_by_provider",
)
PROVIDER_NAMES = ("Harborline", "Tessera", "Quillon", "Marigold", "Lantern", "Verdant")

COVERAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "Harborline": (("MX", "SPEI"), ("BR", "PIX"), ("US", "ACH")),
    "Tessera": (("MX", "SPEI"), ("PH", "InstaPay"), ("PH", "PESONet")),
    "Quillon": (("BR", "PIX"), ("DE", "SCT"), ("DE", "SCT Inst")),
    "Marigold": (("PH", "InstaPay"), ("PH", "PESONet"), ("US", "ACH"), ("US", "Fedwire")),
    "Lantern": (("MX", "SPEI"), ("DE", "SCT"), ("DE", "SCT Inst")),
    "Verdant": (("US", "ACH"), ("US", "Fedwire"), ("BR", "PIX")),
}

# Raw status word -> unified status. "returned" words map to the return event, not a status.
STATUS_VOCABULARY: dict[str, dict[str, str]] = {
    "Harborline": {
        "PENDING": "submitted",
        "SENT": "processing",
        "COMPLETE": "settled",
        "REJECTED": "failed",
        "CANCELLED": "cancelled",
        "RETURNED": "return",
    },
    "Tessera": {
        "100": "submitted",
        "200": "processing",
        "300": "settled",
        "400": "failed",
        "900": "cancelled",
        "350": "return",
    },
    "Quillon": {
        "accepted": "submitted",
        "in_progress": "processing",
        "settled": "settled",
        "rejected": "failed",
        "cancelled": "cancelled",
        "recalled": "return",
    },
    "Marigold": {
        "created": "submitted",
        "processing": "processing",
        "paid_out": "settled",
        "failed": "failed",
        "voided": "cancelled",
        "reversed": "return",
    },
    "Lantern": {
        "RECIBIDO": "submitted",
        "EN_PROCESO": "processing",
        "LIQUIDADO": "settled",
        "RECHAZADO": "failed",
        "CANCELADO": "cancelled",
        "DEVUELTO": "return",
    },
    "Verdant": {
        "queued": "submitted",
        "in flight": "processing",
        "delivered": "settled",
        "failed": "failed",
        "canceled": "cancelled",
        "returned": "return",
    },
}

# Raw failure code -> unified reason. Verdant has no codes: its free text maps by keyword.
REASON_VOCABULARY: dict[str, dict[str, str]] = {
    "Harborline": {
        "ACCOUNT_INVALID": "invalid_account",
        "ACCOUNT_CLOSED": "account_closed",
        "NAME_MISMATCH": "beneficiary_name_mismatch",
        "OVER_LIMIT": "limit_exceeded",
        "COMPLIANCE_REVIEW": "compliance_hold",
        "DECLINED": "provider_rejected",
        "RAIL_DOWN": "rail_unavailable",
        "NO_FLOAT": "insufficient_float",
        "DUPLICATE": "duplicate_submission",
        "CANCEL_CLIENT": "cancelled_by_operator",
        "CANCEL_PROVIDER": "cancelled_by_provider",
    },
    "Tessera": {
        "E401": "invalid_account",
        "E402": "account_closed",
        "E403": "beneficiary_name_mismatch",
        "E410": "limit_exceeded",
        "E420": "compliance_hold",
        "E430": "provider_rejected",
        "E440": "rail_unavailable",
        "E450": "insufficient_float",
        "E460": "duplicate_submission",
        "E901": "cancelled_by_operator",
        "E902": "cancelled_by_provider",
    },
    "Quillon": {
        "AC01": "invalid_account",
        "AC04": "account_closed",
        "BE04": "beneficiary_name_mismatch",
        "AM02": "limit_exceeded",
        "RR04": "compliance_hold",
        "MS03": "provider_rejected",
        "AG01": "rail_unavailable",
        "AM04": "insufficient_float",
        "DUPL": "duplicate_submission",
        "CUST": "cancelled_by_operator",
        "AG02": "cancelled_by_provider",
    },
    "Marigold": {
        "account_not_found": "invalid_account",
        "account_closed": "account_closed",
        "name_mismatch": "beneficiary_name_mismatch",
        "limit": "limit_exceeded",
        "compliance": "compliance_hold",
        "declined": "provider_rejected",
        "rail_unavailable": "rail_unavailable",
        "insufficient_funds": "insufficient_float",
        "duplicate": "duplicate_submission",
        "voided_by_client": "cancelled_by_operator",
        "voided_by_partner": "cancelled_by_provider",
    },
    "Lantern": {
        "CUENTA_INVALIDA": "invalid_account",
        "CUENTA_CERRADA": "account_closed",
        "NOMBRE_NO_COINCIDE": "beneficiary_name_mismatch",
        "LIMITE_EXCEDIDO": "limit_exceeded",
        "REVISION_CUMPLIMIENTO": "compliance_hold",
        "RECHAZADO_POR_BANCO": "provider_rejected",
        "RED_NO_DISPONIBLE": "rail_unavailable",
        "SIN_FONDOS": "insufficient_float",
        "DUPLICADO": "duplicate_submission",
        "CANCELADO_POR_CLIENTE": "cancelled_by_operator",
        "CANCELADO_POR_PROVEEDOR": "cancelled_by_provider",
    },
    "Verdant": {},
}

# Verdant's free text: the first keyword found (in this order) decides the unified reason.
VERDANT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("duplicate", "duplicate_submission"),
    ("closed", "account_closed"),
    ("not found", "invalid_account"),
    ("invalid", "invalid_account"),
    ("name", "beneficiary_name_mismatch"),
    ("limit", "limit_exceeded"),
    ("compliance", "compliance_hold"),
    ("hold", "compliance_hold"),
    ("unavailable", "rail_unavailable"),
    ("outage", "rail_unavailable"),
    ("float", "insufficient_float"),
    ("liquidity", "insufficient_float"),
    ("client cancel", "cancelled_by_operator"),
    ("partner cancel", "cancelled_by_provider"),
    ("declined", "provider_rejected"),
)
VERDANT_TEXTS: dict[str, tuple[str, ...]] = {
    "invalid_account": ("account number not found at bank", "invalid account details"),
    "account_closed": ("beneficiary account closed",),
    "beneficiary_name_mismatch": ("name on account does not match",),
    "limit_exceeded": ("amount over the daily limit",),
    "compliance_hold": ("compliance review hold",),
    "provider_rejected": ("declined by receiving bank",),
    "rail_unavailable": ("rail unavailable at this time", "network outage, retry later"),
    "insufficient_float": ("insufficient float on the corridor", "liquidity shortfall"),
    "duplicate_submission": ("duplicate of an earlier instruction",),
    "cancelled_by_operator": ("client cancel request honoured",),
    "cancelled_by_provider": ("partner cancel after review",),
}
# Free text that matches no keyword: maps to provider_rejected and is flagged reason_unmapped.
VERDANT_UNMAPPED_TEXTS: tuple[str, ...] = (
    "returned by correspondent, code 77",
    "processing exception, see ticket",
    "beneficiary bank gave no reason",
    "could not complete, contact support",
)


def map_verdant_text(text: str) -> tuple[str, bool]:
    """(unified reason, unmapped flag) for a Verdant free-text reason."""
    low = text.lower()
    for key, reason in VERDANT_KEYWORDS:
        if key in low:
            return reason, False
    return "provider_rejected", True


def raw_status_for(provider: str, unified: str) -> str:
    for raw, u in STATUS_VOCABULARY[provider].items():
        if u == unified:
            return raw
    raise KeyError((provider, unified))


def raw_reason_for(provider: str, reason: str, rng: random.Random) -> str:
    if provider == "Verdant":
        return rng.choice(VERDANT_TEXTS[reason])
    for raw, u in REASON_VOCABULARY[provider].items():
        if u == reason:
            return raw
    raise KeyError((provider, reason))


@dataclass
class ProviderParams:
    name: str
    # (corridor, rail) -> settlement (median minutes, log-sigma) on 24/7 rails; Fedwire minutes
    settle_median_min: dict[tuple[str, str], float] = field(default_factory=dict)
    settle_sigma: dict[tuple[str, str], float] = field(default_factory=dict)
    baseline_failure: dict[tuple[str, str], float] = field(default_factory=dict)
    return_rate: dict[tuple[str, str], float] = field(default_factory=dict)
    retry_share: float = 0.7
    statement_lag_s: int = 3600
    webhook_duplicate: float = 0.02
    webhook_out_of_order: float = 0.01
    webhook_late: float = 0.01
    webhook_drop: float = 0.005
    webhook_invalid_sig: float = 0.001
    fee_flat: dict[tuple[str, str], int] = field(default_factory=dict)  # minor units of dst
    fee_bps: dict[tuple[str, str], int] = field(default_factory=dict)
    markup: dict[str, float] = field(default_factory=dict)  # corridor -> fraction
    weight: dict[tuple[str, str], float] = field(default_factory=dict)


FEE_PUBLISHED: dict[str, tuple[int, str]] = {
    # rail -> (published flat fee in minor units, currency), from sources/fees.json
    "InstaPay": (1500, "PHP"),
    "PESONet": (1000, "PHP"),
    "SCT": (100, "EUR"),
    "SCT Inst": (90, "EUR"),
    "Fedwire": (400, "USD"),
    "ACH": (0, "USD"),
    "SPEI": (800, "MXN"),
    "PIX": (300, "BRL"),
}


def _perturb_fee(published: int, rng: random.Random, excluded_cents: set[int]) -> int:
    """A flat fee near the published figure that equals no published or excluded figure."""
    for _ in range(100):
        delta = rng.choice([-37, -23, -17, 13, 19, 27, 41, 53])
        v = max(5, published + delta)
        if v != published and v not in excluded_cents:
            return v
    raise RuntimeError("could not perturb fee")


def draw_markup(
    rng: random.Random, band: tuple[float, float], excluded_pct: frozenset[float]
) -> float:
    """One markup from the band, re-drawn until it sits at least 0.005 points from every excluded
    figure (in percent, two decimals)."""
    lo, hi = band
    for _ in range(1000):
        pct = round(rng.uniform(lo, hi), 4)
        if all(abs(pct - ex) >= 0.005 for ex in excluded_pct):
            return pct / 100.0
    raise RuntimeError("could not draw a markup off the exclusion list")


def draw_providers(
    seed: int,
    band: tuple[float, float],
    excluded_pct: frozenset[float],
    excluded_amounts: frozenset[float],
    floors: dict[str, float] | None = None,
) -> dict[str, ProviderParams]:
    """The six providers' parameters from the seed. ``floors`` gives, per corridor, the lower
    end of that corridor's markup band in percent (the world's band end raised to the corridor's
    quoted market spread); a corridor without one uses the band's own lower end."""
    rng = random.Random(f"{seed}:providers")
    floors = floors or {}
    excluded_cents = {int(round(a * 100)) for a in excluded_amounts}
    out: dict[str, ProviderParams] = {}
    lags = {
        "Harborline": 1.0,
        "Tessera": 1.5,
        "Quillon": 1.5,
        "Marigold": 0.5,
        "Lantern": 2.5,
        "Verdant": 2.0,
    }
    for name in PROVIDER_NAMES:
        p = ProviderParams(name=name)
        p.retry_share = round(rng.uniform(0.60, 0.80), 3)
        p.statement_lag_s = int(lags[name] * 3600 + rng.randint(0, 1800))
        p.webhook_duplicate = round(rng.uniform(0.01, 0.03), 4)
        p.webhook_out_of_order = round(rng.uniform(0.005, 0.02), 4)
        p.webhook_late = round(rng.uniform(0.005, 0.015), 4)
        p.webhook_drop = round(rng.uniform(0.002, 0.01), 4)
        p.webhook_invalid_sig = round(rng.uniform(0.0005, 0.002), 4)
        for corridor, rail in COVERAGE[name]:
            key = (corridor, rail)
            if rail in ("SPEI", "PIX", "SCT Inst"):
                med = rng.uniform(0.2, 2.5)
            elif rail == "InstaPay":
                med = rng.uniform(2.0, 8.0)
            elif rail == "Fedwire":
                med = rng.uniform(25.0, 90.0)
            else:
                med = 0.0
            p.settle_median_min[key] = round(med, 3)
            p.settle_sigma[key] = round(rng.uniform(0.4, 0.9), 3)
            p.baseline_failure[key] = round(rng.uniform(0.02, 0.06), 4)
            p.return_rate[key] = round(rng.uniform(0.004, 0.012), 4)
            published, _cur = FEE_PUBLISHED[rail]
            p.fee_flat[key] = (
                _perturb_fee(published, rng, excluded_cents)
                if published
                else rng.choice([15, 25, 35])
            )
            p.fee_bps[key] = rng.choice([b for b in (8, 12, 15, 18, 22, 25, 28, 35) if round(b / 100, 2) not in excluded_pct])
            p.weight[key] = round(rng.uniform(0.6, 1.4), 3)
        for corridor in sorted({c for c, _ in COVERAGE[name]}):
            corridor_band = (max(band[0], floors.get(corridor, band[0])), band[1])
            p.markup[corridor] = draw_markup(rng, corridor_band, excluded_pct)
        out[name] = p
    return out


def providers_for(corridor: str, rail: str) -> tuple[str, ...]:
    return tuple(n for n in PROVIDER_NAMES if (corridor, rail) in COVERAGE[n])
