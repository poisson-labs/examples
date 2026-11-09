"""The world generator: a pure function of the seed and the frozen source tables.

A synthetic back-office payments world with invented providers; no real company's data.

Everything below is drawn from `random.Random` streams seeded from the world seed and a stream
name, in a fixed order, so one seed gives one world. No wall clock, no locale, no filesystem
order and no dict-order dependence reaches the output.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import BAND_SENTENCE, FX_PROXY_SENTENCE, SYNTHETIC_SENTENCE
from .clock import (
    CALENDAR_RAILS,
    CLOCK_RAILS,
    CORRIDOR_RAILS,
    DAY,
    HISTORY_END,
    HOUR,
    MONITORING_START,
    OPERATOR_OFFSET,
    T0,
    WINDOW_END,
    WINDOW_START,
    ZONE_OFFSET,
    clock_unit,
    elapsed_on_clock,
    fedwire_settlement,
    iso_utc,
    local_date,
    local_midnight,
    next_cutoff,
    operator_week,
    pix_night_queue,
    settlement_time,
    ts,
    week_bounds,
)
from .context import render_context
from .digest import canonical_digests
from .providers import (
    COVERAGE,
    PROVIDER_NAMES,
    TERMINAL,
    VERDANT_UNMAPPED_TEXTS,
    ProviderParams,
    draw_providers,
    map_verdant_text,
    providers_for,
    raw_reason_for,
    raw_status_for,
)
from .rules import (
    PROFILE_MIN_COUNT,
    RAIL_LIMIT_MINOR,
    RECORD_ITEMS,
    RFI_REQUEST_TYPES,
    PairState,
    correct_resolution,
    correct_rfi_action,
    evaluate_degraded,
    evaluate_stuck,
    glossary_answer,
    percentile,
    route,
)
from .rules import (
    STATUS_COUNTS_LOOKBACK_S as _SCL,
)
from .sources import CORRIDOR_CURRENCY, MarkupFloor, Sources, load_sources, markup_floors

GENERATOR_VERSION = "0.1.0"
MARKUP_BAND_PCT = (0.04, 0.33)
DEFAULT_PAYMENTS = 60_000
TEST_SEED = 7
TEST_PAYMENTS = 2_000
DEMO_SEED = 2026

CORRIDOR_MIX = {"MX": 0.34, "BR": 0.22, "PH": 0.16, "DE": 0.14, "US": 0.14}
AMOUNT_MEDIAN_USD = {"MX": 420, "BR": 380, "PH": 260, "DE": 1900, "US": 2400}
AMOUNT_SIGMA = 0.9
PLATFORM_COUNT = 12
END_USERS = 2000
TEMPLATE_PLATFORM = "plt_03"
TEMPLATE_PROVIDER = "Harborline"
SECOND_CONTRADICTION_PROVIDER = "Tessera"
RFI_PER_PAYMENT = 2400 / 60000
FX_TOLERANCE_BPS = 30
PASS_HOUR = 6
POLL_DELAY_CLOCK_S = 24 * HOUR
# Read-only connections that answer the questions by the rule function after the tables are written.
RULE_ANSWER_READERS = 4
POLL_DELAY_OPPORTUNITIES = 2
ASSUME_FAILED_AFTER_S = 48 * HOUR
SUMMARY_REFRESHED_AT = ts(2026, 10, 2, 18, 0, 0)

FAILURE_REASON_WEIGHTS = {
    "invalid_account": 22,
    "account_closed": 10,
    "beneficiary_name_mismatch": 14,
    "limit_exceeded": 5,
    "compliance_hold": 8,
    "provider_rejected": 18,
    "rail_unavailable": 6,
    "insufficient_float": 5,
    "duplicate_submission": 4,
    "cancelled_by_operator": 4,
    "cancelled_by_provider": 4,
}
RETURN_REASONS = (
    "beneficiary_refused",
    "account_closed_after_credit",
    "unauthorised_debit",
    "duplicate_credit",
)

# Hard-case plant counts at full volume (spec section 13 minimums with a margin).
PLANT_FULL = {
    "H1": 52,
    "H2": 26,
    "H3": 39,
    "H4_true": 33,
    "H4_false": 33,
    "H5": 39,
    "H8": 52,
    "H12": 78,
    "H13": 65,
    "H14": 20,
    "H15": 52,
    "H18": 52,
    "H20": 26,
    "H21": 130,
    "H22": 20,
    "status_requery": 20,
    "amount_fx": 24,
    "amount_escalate": 16,
    "missing_at_requery": 20,
    "missing_at_escalate": 16,
    "missing_in_create": 24,
    "missing_in_escalate": 16,
    "late_cancel_adopt": 24,
    "late_return_escalate": 16,
    "stuck_clock": 40,
    "stuck_calendar": 30,
    "stuck_degraded": 20,
    "settled_before_t": 20,
    "limit_fail": 24,
}


def _scale_counts(payments: int) -> dict[str, int]:
    s = payments / DEFAULT_PAYMENTS
    return {k: max(2, math.ceil(v * s)) for k, v in PLANT_FULL.items()}


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------


@dataclass
class Attempt:
    attempt_id: str
    payment_id: str
    n: int
    corridor: str
    provider: str
    rail: str
    created_at: int
    amount_usd_cents: int
    amount_dst: int
    fee_minor: int
    fee_booked: bool
    fx_mid: float
    fx_markup: float
    fx_quote: float
    client_reference: str
    submitted_at: int | None = None
    provider_reference: str | None = None
    # the provider's own outcome
    prov_status: str | None = None  # settled / failed / cancelled, None if open at T0
    prov_terminal_at: int | None = None
    settlement_reference: str | None = None
    failure_reason: str | None = None
    raw_failure: str | None = None
    reason_unmapped: bool = False
    # returns
    return_at: int | None = None
    return_reason: str | None = None
    return_reference: str | None = None
    return_amount: int | None = None
    # plants and labels
    plant: str | None = None
    hard: list[str] = field(default_factory=list)
    hold_until: int | None = None
    # ledger timeline: (t, status, source, reference, failure_code); built later
    timeline: list[tuple[int, str, str, str | None, str | None]] = field(default_factory=list)
    status_source: str = "webhook"
    operator_events: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    ledger_return_at: int | None = None
    recon_booked_at: int | None = None
    poll_failed_at: int | None = None
    fee_booked_at: int | None = None
    deleted: bool = False
    stuck_as_of: int | None = None
    # statement entries: list of dicts with entry_at, status, amount, fee, reference, ...
    entries: list[dict[str, Any]] = field(default_factory=list)
    prov_history: list[tuple[int, str]] = field(
        default_factory=list
    )  # (t, raw status) as the provider reported


@dataclass
class Payment:
    payment_id: str
    platform: str
    end_user: str
    corridor: str
    currency: str
    amount_usd_cents: int
    created_at: int
    attempts: list[Attempt] = field(default_factory=list)
    hard: list[str] = field(default_factory=list)


@dataclass
class Event:
    event_id: str
    attempt_id: str
    provider: str
    provider_event_id: str
    occurred_at: int
    raw_status: str
    unified: str  # unified status or 'return'
    source: str  # webhook / poll / operator / statement
    delivered_at: int | None
    signature_valid: bool
    applied: bool
    apply_result: str
    reference: str | None
    failure_code: str | None
    payload: dict[str, Any]


class Streams:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._cache: dict[str, random.Random] = {}

    def __call__(self, name: str) -> random.Random:
        r = self._cache.get(name)
        if r is None:
            r = random.Random(f"backoffice-world:{self.seed}:{name}")
            self._cache[name] = r
        return r


def _lognormal_minutes(rng: random.Random, median: float, sigma: float) -> int:
    return max(1, int(round(median * math.exp(rng.gauss(0.0, sigma)) * 60)))


def _weighted(rng: random.Random, items: dict[str, float]) -> str:
    keys = list(items)
    return rng.choices(keys, weights=[items[k] for k in keys], k=1)[0]


# ---------------------------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------------------------


class Generator:
    def __init__(self, seed: int, sources: Sources, payments: int) -> None:
        self.seed = seed
        self.src = sources
        self.n_payments = payments
        self.S = Streams(seed)
        self.counts = _scale_counts(payments)
        self.profile_min_count = PROFILE_MIN_COUNT if payments >= DEFAULT_PAYMENTS // 2 else max(7, math.ceil(PROFILE_MIN_COUNT * payments / DEFAULT_PAYMENTS))
        excluded_pct = frozenset(v for v in sources.excluded_figures)
        # each corridor's markup band is floored at the corridor's saved market spread
        self.floors: dict[str, MarkupFloor] = markup_floors(
            sources.sanity, MARKUP_BAND_PCT, excluded_pct
        )
        self.providers: dict[str, ProviderParams] = draw_providers(
            seed,
            MARKUP_BAND_PCT,
            excluded_pct,
            sources.excluded_figures,
            floors={c: f.floor_pct for c, f in self.floors.items()},
        )
        self.cal = sources.calendars
        self.payments: list[Payment] = []
        self.attempts: list[Attempt] = []
        self.attempt_by_id: dict[str, Attempt] = {}
        self.events: list[Event] = []
        self.platforms: list[dict[str, Any]] = []
        self.users: list[dict[str, Any]] = []
        self.user_by_id: dict[str, dict[str, Any]] = {}
        self.episodes: list[dict[str, Any]] = []
        self.webhook_incidents: list[dict[str, Any]] = []
        self.profiles: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.baseline: dict[tuple[str, str, str], float] = {}
        self.passes: list[dict[str, Any]] = []
        self.breaks: list[dict[str, Any]] = []
        self.orphan_entries: list[dict[str, Any]] = []
        self.rfis: list[dict[str, Any]] = []
        self.stuck_cases: list[dict[str, Any]] = []
        self.reroute_cases: list[dict[str, Any]] = []
        self.questions: list[dict[str, Any]] = []
        self.summary_rows: list[dict[str, Any]] = []
        self.hard_counts: dict[str, int] = {}
        self.degraded_hours: dict[tuple[str, str, str], list[int]] = {}
        self.incidents: list[dict[str, Any]] = []
        self._ref_counter = 0
        self._event_counter = 0

    # ---- helpers ----
    def ref(self, prefix: str) -> str:
        self._ref_counter += 1
        return f"{prefix}{self._ref_counter:07d}"

    def quote(self, provider: str, corridor: str, t: int) -> tuple[float, float, float]:
        if corridor == "US":
            return 1.0, 0.0, 1.0
        mid = self.src.fixing_on(corridor, t).rate
        markup = self.providers[provider].markup[corridor]
        return mid, markup, mid * (1.0 + markup)

    def fee(self, provider: str, corridor: str, rail: str, amount_dst: int) -> int:
        p = self.providers[provider]
        return p.fee_flat[(corridor, rail)] + (amount_dst * p.fee_bps[(corridor, rail)]) // 10_000

    # ---- 1. platforms, users, identity records ----
    def build_users(self) -> None:
        rng = self.S("users")
        sizes = [0.07, 0.07, 0.30] + [0.07] * 7 + [0.035, 0.035]
        corridor_tilt = {}
        for i in range(PLATFORM_COUNT):
            pid = f"plt_{i + 1:02d}"
            tilt = dict(CORRIDOR_MIX)
            fav = rng.choice(list(CORRIDOR_MIX))
            tilt[fav] *= 1.8
            if pid == TEMPLATE_PLATFORM:
                tilt = {"MX": 0.70, "BR": 0.15, "PH": 0.05, "DE": 0.05, "US": 0.05}
            corridor_tilt[pid] = tilt
            self.platforms.append(
                {
                    "platform_id": pid,
                    "name": f"Platform {i + 1:02d}",
                    "share": sizes[i],
                    "corridors": tilt,
                }
            )
        order = list(range(PLATFORM_COUNT))
        employment = [
            "employed",
            "self_employed",
            "student",
            "retired",
            "unemployed",
            "business_owner",
        ]
        sources_of_funds = [
            "salary",
            "savings",
            "business_income",
            "pension",
            "family_support",
            "investment",
        ]
        for ui in range(END_USERS):
            uid = f"usr_{ui + 1:04d}"
            # allocate by platform share
            r = rng.random()
            acc = 0.0
            platform = self.platforms[-1]["platform_id"]
            for i in order:
                acc += self.platforms[i]["share"]
                if r < acc:
                    platform = self.platforms[i]["platform_id"]
                    break
            emp = rng.choice(employment)
            sof = rng.choice(sources_of_funds)
            items: dict[str, dict[str, Any]] = {}
            for item in RECORD_ITEMS:
                present = rng.random() > 0.04
                expires = None
                if item == "doc_identity":
                    y = rng.choice([2025, 2026, 2027, 2028, 2029, 2030])
                    expires = dt.date(y, rng.randint(1, 12), rng.randint(1, 28)).isoformat()
                if item == "doc_proof_of_address":
                    expires = (dt.date(2026, rng.randint(1, 12), rng.randint(1, 28))).isoformat()
                items[item] = {"present": present, "expires": expires}
            items["item_employment"]["value"] = emp
            items["item_source_of_funds"]["value"] = sof
            contradictions: list[list[str]] = []
            self.users.append(
                {
                    "user_id": uid,
                    "platform": platform,
                    "name": f"End user {ui + 1:04d}",
                    "items": items,
                    "contradictions": contradictions,
                    "template_contradiction": False,
                }
            )
        # the template platform's 7% contradiction, and under 1% elsewhere of two other shapes
        template_users = [u for u in self.users if u["platform"] == TEMPLATE_PLATFORM]
        rng.shuffle(template_users)
        n_contra = max(2, int(round(len(template_users) * 0.07)))
        for u in template_users[:n_contra]:
            u["items"]["item_employment"]["value"] = "student"
            u["items"]["item_source_of_funds"]["value"] = "salary"
            u["items"]["item_employment"]["present"] = True
            u["items"]["item_source_of_funds"]["present"] = True
            u["contradictions"].append(["item_employment", "item_source_of_funds"])
            u["template_contradiction"] = True
        others = [u for u in self.users if u["platform"] != TEMPLATE_PLATFORM]
        rng.shuffle(others)
        n_other = max(1, int(len(others) * 0.006))
        size_of: dict[str, int] = {}
        for u in others:
            size_of[u["platform"]] = size_of.get(u["platform"], 0) + 1
        room = {pl: int(n * 0.009) for pl, n in size_of.items()}
        chosen: list[dict[str, Any]] = []
        for u in others:
            if room[u["platform"]] > 0 and len(chosen) < n_other:
                room[u["platform"]] -= 1
                chosen.append(u)
        for k, u in enumerate(chosen):
            pair = (
                ["item_date_of_birth", "doc_identity"]
                if k % 2 == 0
                else ["item_address", "doc_proof_of_address"]
            )
            u["items"][pair[0]]["present"] = True
            u["items"][pair[1]]["present"] = True
            u["contradictions"].append(pair)
        for u in self.users:
            self.user_by_id[u["user_id"]] = u

    # ---- 2. episodes and incidents (planted before payments, since they shape them) ----
    def build_episodes(self) -> None:
        rng = self.S("episodes")

        def ep(provider: str, corridor: str, rail: str, start: int, end: int, label: str) -> None:
            self.episodes.append(
                {
                    "episode_id": f"deg_{len(self.episodes) + 1:02d}",
                    "provider": provider,
                    "corridor": corridor,
                    "rail": rail,
                    "start": start,
                    "end": end,
                    "failure_share": round(rng.uniform(0.40, 0.60), 3),
                    "label": label,
                    "in_monitoring": start >= MONITORING_START,
                }
            )

        ep("Harborline", "MX", "SPEI", ts(2026, 9, 15, 9, 0), ts(2026, 9, 16, 15, 0), "monitoring")
        ep(
            "Tessera",
            "PH",
            "InstaPay",
            ts(2026, 9, 18, 14, 0),
            ts(2026, 9, 20, 2, 0),
            "straddles_weekend",
        )
        ep(
            "Marigold",
            "PH",
            "InstaPay",
            ts(2026, 9, 19, 8, 0),
            ts(2026, 9, 19, 22, 0),
            "overlap_calendar_alternative",
        )
        ep("Quillon", "BR", "PIX", ts(2026, 9, 26, 14, 0), ts(2026, 9, 27, 2, 0), "overlap_no_healthy")
        ep(
            "Verdant",
            "BR",
            "PIX",
            ts(2026, 9, 25, 20, 0),
            ts(2026, 9, 27, 10, 0),
            "straddles_weekend",
        )
        ep(
            "Harborline",
            "BR",
            "PIX",
            ts(2026, 9, 26, 12, 0),
            ts(2026, 9, 27, 4, 0),
            "overlap_no_healthy",
        )
        ep(
            "Marigold",
            "PH",
            "InstaPay",
            ts(2026, 8, 20, 20, 0),
            ts(2026, 8, 22, 8, 0),
            "spans_ph_holiday",
        )
        ep("Verdant", "BR", "PIX", ts(2026, 7, 28, 7, 0), ts(2026, 7, 29, 19, 0), "history")
        # three webhook incidents (the provider's webhooks silent; its payouts API keeps settling)
        self.webhook_incidents = [
            {
                "incident_id": "whk_01",
                "provider": "Quillon",
                "start": ts(2026, 9, 22, 9, 0),
                "end": ts(2026, 9, 22, 17, 30),
            },
            {
                "incident_id": "whk_02",
                "provider": "Marigold",
                "start": ts(2026, 7, 16, 10, 0),
                "end": ts(2026, 7, 16, 16, 0),
            },
            {
                "incident_id": "whk_03",
                "provider": "Lantern",
                "start": ts(2026, 9, 28, 9, 0),
                "end": ts(2026, 9, 28, 12, 30),
            },
        ]

    def episode_at(self, provider: str, corridor: str, rail: str, t: int) -> dict[str, Any] | None:
        for e in self.episodes:
            if (
                e["provider"] == provider
                and e["corridor"] == corridor
                and e["rail"] == rail
                and e["start"] <= t < e["end"]
            ):
                return e
        return None

    def webhook_silent(self, provider: str, t: int) -> dict[str, Any] | None:
        for w in self.webhook_incidents:
            if w["provider"] == provider and w["start"] <= t < w["end"]:
                return w
        return None

    # ---- 3. payments and attempts ----
    def build_payments(self) -> None:
        rng = self.S("payments")
        units = []
        d = local_date(WINDOW_START, OPERATOR_OFFSET)
        end = local_date(WINDOW_END, OPERATOR_OFFSET)
        while d <= end:
            units.append((d, 1.6 if d.weekday() < 5 else 1.0))
            d = d + dt.timedelta(days=1)
        per_unit = self.n_payments / sum(w for _, w in units)
        users_by_platform: dict[str, list[dict[str, Any]]] = {}
        for u in self.users:
            users_by_platform.setdefault(u["platform"], []).append(u)
        platform_weights = {p["platform_id"]: p["share"] for p in self.platforms}
        pid = 0
        for day, w in units:
            n = int(round(per_unit * w * rng.uniform(0.9, 1.1)))
            base = local_midnight(day, OPERATOR_OFFSET)
            for _ in range(n):
                if rng.random() < 0.65:
                    sec = int(min(max(rng.gauss(13 * HOUR, 3 * HOUR), 7 * HOUR), 20 * HOUR - 1))
                else:
                    sec = rng.randrange(0, DAY)
                created = base + sec
                platform = _weighted(rng, platform_weights)
                user = rng.choice(users_by_platform[platform])
                corridor = _weighted(rng, self.platforms[int(platform[-2:]) - 1]["corridors"])
                pid += 1
                self._new_payment(rng, pid, platform, user["user_id"], corridor, created, None)
        self.payments.sort(key=lambda p: (p.created_at, p.payment_id))

    def _amount_under_limit(self, amount_cents: int, corridor: str, rail: str, created: int) -> int:
        """A planted payout forced onto `rail` never exceeds the rail's limit: the amount is
        capped at seven tenths of the limit, converted at the day's mid, so the limit test holds
        at every seed, not only where the draw happened to fall below it."""
        limit = RAIL_LIMIT_MINOR.get(rail)
        if limit is None:
            return amount_cents
        mid = 1.0 if corridor == "US" else self.src.fixing_on(corridor, created).rate
        return min(amount_cents, int(limit * 0.7 / mid))

    def _amount_usd(self, rng: random.Random, corridor: str) -> int:
        med = AMOUNT_MEDIAN_USD[corridor]
        amt = med * math.exp(rng.gauss(0.0, AMOUNT_SIGMA))
        if corridor == "US" and rng.random() < 0.005:
            amt = rng.uniform(100_000, 600_000)
        amt = min(amt, 2_500_000)
        return max(500, int(round(amt * 100)))

    def _new_payment(
        self,
        rng: random.Random,
        pid: int,
        platform: str,
        user_id: str,
        corridor: str,
        created: int,
        amount_cents: int | None,
    ) -> Payment:
        amount = amount_cents if amount_cents is not None else self._amount_usd(rng, corridor)
        payment = Payment(
            payment_id=f"pay_{pid:06d}",
            platform=platform,
            end_user=user_id,
            corridor=corridor,
            currency=CORRIDOR_CURRENCY[corridor],
            amount_usd_cents=amount,
            created_at=created,
        )
        self.payments.append(payment)
        return payment

    def choose_rail(
        self, rng: random.Random, corridor: str, amount_dst_est: int, amount_usd: int
    ) -> str:
        rails = CORRIDOR_RAILS[corridor]
        if len(rails) == 1:
            return rails[0]
        instapay_limit = RAIL_LIMIT_MINOR["InstaPay"] or 0
        sct_inst_limit = RAIL_LIMIT_MINOR["SCT Inst"] or 0
        ach_limit = RAIL_LIMIT_MINOR["ACH"] or 0
        if corridor == "PH":
            if amount_dst_est * 1.004 > instapay_limit:
                return "PESONet"
            return "PESONet" if rng.random() < 0.15 else "InstaPay"
        if corridor == "DE":
            if amount_dst_est * 1.004 > sct_inst_limit:
                return "SCT"
            return "SCT" if rng.random() < 0.20 else "SCT Inst"
        if corridor == "US":
            if amount_usd * 1.004 > ach_limit:
                return "Fedwire"
            if amount_usd >= 50_000 * 100:
                return "Fedwire" if rng.random() < 0.6 else "ACH"
            return "Fedwire" if rng.random() < 0.10 else "ACH"
        raise ValueError(corridor)

    def choose_provider(
        self,
        rng: random.Random,
        corridor: str,
        rail: str,
        exclude: str | None = None,
        platform: str | None = None,
    ) -> str:
        cands = [p for p in providers_for(corridor, rail) if p != exclude]
        if not cands:
            cands = list(providers_for(corridor, rail))
        weights = []
        for p in cands:
            w = self.providers[p].weight[(corridor, rail)]
            if platform == TEMPLATE_PLATFORM and p == TEMPLATE_PROVIDER:
                w *= 3.0
            weights.append(w)
        return rng.choices(cands, weights=weights, k=1)[0]

    def make_attempt(
        self,
        rng: random.Random,
        payment: Payment,
        n: int,
        created: int,
        provider: str | None = None,
        rail: str | None = None,
        exclude_provider: str | None = None,
    ) -> Attempt:
        corridor = payment.corridor
        est_mid = 1.0 if corridor == "US" else self.src.fixing_on(corridor, created).rate
        est_dst = int(round(payment.amount_usd_cents * est_mid))
        if rail is None:
            rail = self.choose_rail(rng, corridor, est_dst, payment.amount_usd_cents)
        if provider is None:
            provider = self.choose_provider(
                rng, corridor, rail, exclude=exclude_provider, platform=payment.platform
            )
        mid, markup, q = self.quote(provider, corridor, created)
        amount_dst = int(round(payment.amount_usd_cents * q))
        fee = self.fee(provider, corridor, rail, amount_dst)
        a = Attempt(
            attempt_id=f"att_{payment.payment_id[4:]}_{n}",
            payment_id=payment.payment_id,
            n=n,
            corridor=corridor,
            provider=provider,
            rail=rail,
            created_at=created,
            amount_usd_cents=payment.amount_usd_cents,
            amount_dst=amount_dst,
            fee_minor=fee,
            fee_booked=True,
            fx_mid=mid,
            fx_markup=markup,
            fx_quote=q,
            client_reference=f"CR{payment.payment_id[4:]}{n}",
        )
        payment.attempts.append(a)
        self.attempts.append(a)
        self.attempt_by_id[a.attempt_id] = a
        return a

    def submission_time(self, a: Attempt, rng: random.Random, base: int | None = None) -> int:
        created = a.created_at if base is None else base
        cal = self.cal[a.corridor]
        off = cal.offset
        if a.rail == "PIX" and a.amount_dst > 20_000 * 100:
            q = pix_night_queue(created)
            if q is not None:
                return q + rng.randint(5, 120)
        if a.rail in CALENDAR_RAILS:
            d = local_date(created, off)
            cut = (
                "16:15"
                if a.rail == "ACH"
                else "14:00"
                if a.rail == "PESONet"
                else "16:00"
                if a.rail == "SCT"
                else "17:00"
            )
            h, m = (int(x) for x in cut.split(":"))
            last_cut = ts(d.year, d.month, d.day, h, m, 0, offset=off)
            if not cal.is_banking_day(d) or created > last_cut:
                nxt = cal.next_banking_day(d)
                return ts(nxt.year, nxt.month, nxt.day, 7, 0, 0, offset=off) + rng.randint(0, 1800)
        return created + rng.randint(5, 120)

    def settle_time(self, a: Attempt, rng: random.Random) -> int:
        assert a.submitted_at is not None
        p = self.providers[a.provider]
        key = (a.corridor, a.rail)
        cal = self.cal[a.corridor]
        if a.rail in CLOCK_RAILS:
            return a.submitted_at + _lognormal_minutes(
                rng, p.settle_median_min[key], p.settle_sigma[key]
            )
        if a.rail == "Fedwire":
            minutes = max(
                1,
                int(
                    round(p.settle_median_min[key] * math.exp(rng.gauss(0.0, p.settle_sigma[key])))
                ),
            )
            return fedwire_settlement(a.submitted_at, cal, minutes)
        return settlement_time(a.rail, a.submitted_at, cal) + rng.randint(0, 300)

    def run_lifecycles(self) -> None:
        """Submission, outcome, returns and retries for every payment, in created order."""
        rng = self.S("lifecycle")
        queue: list[Payment] = list(self.payments)
        i = 0
        while i < len(queue):
            payment = queue[i]
            i += 1
            if payment.attempts:
                continue
            self._lifecycle(rng, payment, 1, payment.created_at, None)

    def _lifecycle(
        self,
        rng: random.Random,
        payment: Payment,
        n: int,
        created: int,
        exclude: str | None,
        forced: dict[str, Any] | None = None,
    ) -> Attempt:
        forced = forced or {}
        a = self.make_attempt(
            rng,
            payment,
            n,
            created,
            provider=forced.get("provider"),
            rail=forced.get("rail"),
            exclude_provider=exclude,
        )
        p = self.providers[a.provider]
        key = (a.corridor, a.rail)
        ep = self.episode_at(a.provider, a.corridor, a.rail, created)
        if ep is not None and rng.random() < 0.25 and "hold" not in forced:
            a.hold_until = ep["end"] + rng.randint(300, 1800)
            a.submitted_at = self.submission_time(a, rng, base=a.hold_until)
        else:
            a.submitted_at = self.submission_time(a, rng)
        if a.submitted_at > T0:
            a.submitted_at = None
            return a
        a.provider_reference = self.ref("PR")
        a.prov_history.append((a.submitted_at, raw_status_for(a.provider, "submitted")))
        a.prov_history.append(
            (a.submitted_at + rng.randint(1, 90), raw_status_for(a.provider, "processing"))
        )
        ep_sub = self.episode_at(a.provider, a.corridor, a.rail, a.submitted_at)
        fail_share = p.baseline_failure[key]
        if ep_sub is not None:
            fail_share = ep_sub["failure_share"]
        outcome = forced.get("outcome")
        if outcome is None:
            r = rng.random()
            if r < fail_share:
                outcome = "failed"
            elif r < fail_share + 0.004:
                outcome = "cancelled"
            else:
                outcome = "settled"
        if outcome == "settled":
            t = self.settle_time(a, rng)
            if forced.get("settle_delay_s"):
                delay = int(forced["settle_delay_s"])
                if a.rail in CLOCK_RAILS:
                    t += delay
                elif a.rail == "Fedwire":
                    t = fedwire_settlement(a.submitted_at + delay, self.cal[a.corridor], rng.randint(5, 40))
                else:
                    t = settlement_time(a.rail, a.submitted_at + delay, self.cal[a.corridor]) + rng.randint(0, 300)
            if t <= T0:
                a.prov_status = "settled"
                a.prov_terminal_at = t
                a.settlement_reference = self.ref("ST")
                a.prov_history.append((t, raw_status_for(a.provider, "settled")))
                self._maybe_return(rng, a, forced)
        else:
            if outcome == "failed":
                if ep_sub is not None:
                    reason = "rail_unavailable" if rng.random() < 0.7 else "provider_rejected"
                else:
                    reason = forced.get("reason") or _weighted(
                        rng,
                        {
                            k: v
                            for k, v in FAILURE_REASON_WEIGHTS.items()
                            if not k.startswith("cancelled")
                        },
                    )
                t = a.submitted_at + rng.randint(60, 3 * HOUR)
            else:
                reason = forced.get("reason") or (
                    "cancelled_by_operator" if rng.random() < 0.5 else "cancelled_by_provider"
                )
                t = a.submitted_at + rng.randint(120, 6 * HOUR)
            if t <= T0:
                a.prov_status = outcome
                a.prov_terminal_at = t
                a.failure_reason = reason
                if a.provider == "Verdant" and outcome == "failed" and rng.random() < 0.30:
                    a.raw_failure = rng.choice(VERDANT_UNMAPPED_TEXTS)
                    a.failure_reason, a.reason_unmapped = map_verdant_text(a.raw_failure)
                    a.hard.append("H9")
                else:
                    a.raw_failure = raw_reason_for(a.provider, reason, rng)
                a.prov_history.append((t, raw_status_for(a.provider, outcome)))
                # retries
                retry = forced.get("retry")
                if retry is None:
                    retry = rng.random() < p.retry_share
                if retry and reason not in ("cancelled_by_operator",):
                    nxt = t + rng.randint(120, 6 * HOUR)
                    if nxt <= T0 and n < 4:
                        move = forced.get("move_provider")
                        if move is None:
                            move = rng.random() < 0.30
                        self._lifecycle(
                            rng,
                            payment,
                            n + 1,
                            nxt,
                            a.provider if move else None,
                            {"outcome": forced.get("retry_outcome")}
                            if forced.get("retry_outcome")
                            else None,
                        )
        return a

    def _maybe_return(self, rng: random.Random, a: Attempt, forced: dict[str, Any]) -> None:
        p = self.providers[a.provider]
        rate = p.return_rate[(a.corridor, a.rail)]
        want = forced.get("return")
        if want is None:
            want = rng.random() < rate
        if not want:
            return
        assert a.prov_terminal_at is not None
        cal = self.cal[a.corridor]
        if "return_offset_s" in forced:
            t = a.prov_terminal_at + int(forced["return_offset_s"])
        elif a.rail == "SPEI":
            t = a.prov_terminal_at + rng.randint(1 * DAY, 3 * DAY)
        elif a.rail == "PIX":
            t = a.prov_terminal_at + rng.randint(2 * DAY, 10 * DAY)
        elif a.rail == "InstaPay":
            t = a.prov_terminal_at + rng.randint(600, HOUR)
        elif a.rail == "PESONet":
            nd = cal.next_banking_day(local_date(a.prov_terminal_at, cal.offset))
            t = ts(nd.year, nd.month, nd.day, 10, 0, 0, offset=cal.offset) + rng.randint(
                0, 4 * HOUR
            )
        elif a.rail in ("SCT", "SCT Inst"):
            nd = cal.add_banking_days(
                local_date(a.prov_terminal_at, cal.offset), rng.randint(1, 10)
            )
            t = ts(nd.year, nd.month, nd.day, 10, 0, 0, offset=cal.offset) + rng.randint(
                0, 4 * HOUR
            )
        elif a.rail == "ACH":
            nd = cal.add_banking_days(local_date(a.prov_terminal_at, cal.offset), rng.randint(1, 2))
            t = ts(nd.year, nd.month, nd.day, 9, 0, 0, offset=cal.offset) + rng.randint(0, 6 * HOUR)
        else:  # Fedwire: a new wire back
            t = a.prov_terminal_at + rng.randint(1 * DAY, 5 * DAY)
        if t > T0:
            return
        a.return_at = t
        a.return_reason = forced.get("return_reason") or rng.choice(
            RETURN_REASONS[:2] if a.rail != "ACH" else RETURN_REASONS[:3]
        )
        a.return_reference = self.ref("RT")
        a.return_amount = a.amount_dst
        a.prov_history.append((t, raw_status_for(a.provider, "return")))

    def _daily_pass_between(self, lo: int, hi: int) -> bool:
        """Whether a daily pass (06:00 operator time) runs at a time in [lo, hi)."""
        d = local_date(lo, OPERATOR_OFFSET)
        for _ in range(3):
            at = ts(d.year, d.month, d.day, PASS_HOUR, 0, 0)
            if lo <= at < hi:
                return True
            d = d + dt.timedelta(days=1)
        return False

    # ---- 4. plants (hard cases) ----
    def plant(self) -> None:
        rng = self.S("plants")
        C = self.counts
        settled = [a for a in self.attempts if a.prov_status == "settled" and a.plant is None]
        rng.shuffle(settled)
        mon_settled = [
            a for a in settled if a.submitted_at is not None and a.submitted_at >= MONITORING_START
        ]
        hist_settled = [
            a for a in settled if a.submitted_at is not None and a.submitted_at < MONITORING_START
        ]

        def term(a: Attempt) -> int:
            assert a.prov_terminal_at is not None
            return a.prov_terminal_at

        def take(pool: list[Attempt], k: int, pred: Any = None, margin: int = 6 * DAY) -> list[Attempt]:
            out: list[Attempt] = []
            for a in pool:
                if (
                    a.plant is None
                    and a.prov_terminal_at is not None
                    and a.prov_terminal_at + margin < T0
                    and (pred is None or pred(a))
                ):
                    out.append(a)
                    if len(out) == k:
                        break
            return out

        # H1 amount mismatch: difference equals the fee and sits inside the FX tolerance; fee unbooked
        for a in take(
            settled, C["H1"], margin=DAY, pred=lambda a: a.fee_minor <= (a.amount_dst * FX_TOLERANCE_BPS) // 10_000
        ):
            a.plant = "H1"
            a.hard.append("H1")
            a.fee_booked = False
            a.entries.append({"kind": "amount", "diff": a.fee_minor})
        # H2 same difference, fee booked -> escalate
        for a in take(
            settled, C["H2"], margin=DAY, pred=lambda a: a.fee_minor <= (a.amount_dst * FX_TOLERANCE_BPS) // 10_000
        ):
            a.plant = "H2"
            a.hard.append("H2")
            a.entries.append({"kind": "amount", "diff": a.fee_minor})
        # amount: fx difference within tolerance, fee booked
        for a in take(
            settled,
            C["amount_fx"], margin=DAY,
            pred=lambda a: (a.amount_dst * FX_TOLERANCE_BPS) // 10_000 > a.fee_minor + 5,
        ):
            a.plant = "amount_fx"
            tol = (a.amount_dst * FX_TOLERANCE_BPS) // 10_000
            diff = rng.randint(1, tol)
            if diff == a.fee_minor:
                diff = diff + 1 if diff + 1 <= tol else diff - 1
            a.entries.append({"kind": "amount", "diff": diff})
        # amount: an outside-tolerance difference the provider later corrects to a different one (a new version)
        for a in take(settled, C["amount_escalate"], margin=DAY):
            a.plant = "amount_version"
            tol = (a.amount_dst * FX_TOLERANCE_BPS) // 10_000
            a.entries.append({"kind": "amount", "diff": tol + 300 + rng.randint(1, 500)})
            a.entries.append({"kind": "amount_correction", "diff": tol + 100 + rng.randint(1, 150), "offset": rng.randint(2 * DAY, 4 * DAY)})
        # amount: outside tolerance -> escalate
        for a in take(settled, C["amount_escalate"], margin=DAY):
            a.plant = "amount_escalate"
            tol = (a.amount_dst * FX_TOLERANCE_BPS) // 10_000
            a.entries.append(
                {"kind": "amount", "amount": a.amount_dst - (tol + 100 + rng.randint(1, 500))}
            )
        # H3 late cancel with a settlement reference (escalate) ; late_cancel adopt without one
        for a in take(settled, C["H3"]):
            a.plant = "H3"
            a.hard.append("H3")
            a.return_at = None
            a.prov_history = [h for h in a.prov_history if h[1] != raw_status_for(a.provider, "return")]
            a.entries.append(
                {
                    "kind": "late_cancel",
                    "settlement_reference": a.settlement_reference,
                    "at": term(a) + rng.randint(2 * HOUR, 2 * DAY),
                }
            )
        # H4 duplicates: true duplicates and negative corrections
        for a in take(settled, C["H4_true"]):
            a.plant = "H4_true"
            a.hard.append("H4")
            a.entries.append(
                {
                    "kind": "duplicate",
                    "second_amount": a.amount_dst,
                    "second_reference": self.ref("ST"),
                }
            )
        for a in take(settled, C["H4_false"]):
            a.plant = "H4_false"
            a.hard.append("H4")
            a.entries.append(
                {
                    "kind": "duplicate",
                    "second_amount": -a.amount_dst,
                    "second_reference": self.ref("ST"),
                }
            )
        # H5 our valid webhook says failed; statement says settled with a reference -> escalate
        failed = [
            a
            for a in self.attempts
            if a.prov_status == "failed" and a.plant is None and a.raw_failure
        ]
        rng.shuffle(failed)
        for a in take(failed, C["H5"], margin=DAY):
            a.plant = "H5"
            a.hard.append("H5")
            a.entries.append({"kind": "status", "status": "settled", "reference": self.ref("ST")})
        # H15 completed, later failed: the provider flips to failed after reporting settled
        for a in take(settled, C["H15"]):
            a.plant = "H15"
            a.hard.append("H15")
            a.entries.append(
                {
                    "kind": "status",
                    "status": "failed",
                    "failure_code": raw_reason_for(a.provider, "provider_rejected", rng),
                    "at": term(a) + rng.randint(6 * HOUR, 3 * DAY),
                }
            )
        # status_mismatch requery: the statement entry is older than our last event (a stale entry)
        for a in take(settled, C["status_requery"], margin=DAY, pred=lambda a: term(a) - (a.submitted_at or 0) > 12 * HOUR + self.providers[a.provider].statement_lag_s):
            a.plant = "status_requery"
            stale_at = term(a) - self.providers[a.provider].statement_lag_s - rng.randint(HOUR, 10 * HOUR)
            a.entries.append({"kind": "status_stale", "status": "failed", "reference": None, "failure_code": None, "event_at": stale_at})
            a.prov_history.append((stale_at, raw_status_for(a.provider, "failed")))
            a.prov_history.sort()
        # H8 ours_behind with no provider reference on the statement (requery); needs ours behind: drop the webhook
        for a in take(settled, C["H8"], margin=DAY, pred=lambda a: a.rail in CLOCK_RAILS and a.prov_terminal_at < a.submitted_at + 20 * HOUR):
            a.plant = "H8"
            a.hard.append("H8")
            a.entries.append({"kind": "no_reference"})
        # missing at provider: requery (ours not terminal past the lag) and escalate (ours settled with a reference)
        for a in take(settled, C["missing_at_requery"]):
            a.plant = "missing_at_requery"
            a.entries.append(
                {
                    "kind": "missing_until",
                    "until": term(a)
                    + self.providers[a.provider].statement_lag_s
                    + rng.randint(2 * DAY, 5 * DAY),
                }
            )
        for a in take(settled, C["missing_at_escalate"]):
            a.plant = "missing_at_escalate"
            a.entries.append(
                {
                    "kind": "missing_until",
                    "until": term(a)
                    + self.providers[a.provider].statement_lag_s
                    + rng.randint(2 * DAY, 5 * DAY),
                }
            )
        # missing in ledger: an orphan statement entry (create when it matches a created attempt of ours)
        for a in take(settled, C["missing_in_create"]):
            a.plant = "missing_in_create"
        for a in take(settled, C["missing_in_escalate"]):
            a.plant = "missing_in_escalate"
        # late returns: H13 (weekly, not daily) and escalate (amount differs)
        for a in take(
            mon_settled + hist_settled,
            C["H13"],
            lambda a: (
                a.return_at is None
                and a.rail in ("SPEI", "PIX")
                and a.prov_terminal_at - a.submitted_at < 6 * HOUR
                and a.prov_terminal_at + 13 * DAY < T0
            ),
        ):
            a.plant = "H13"
            a.hard.append("H13")
            a.entries.append(
                {
                    "kind": "return_late",
                    "offset": rng.randint(8 * DAY, 12 * DAY),
                    "amount": a.amount_dst,
                }
            )
        for a in take(
            settled,
            C["late_return_escalate"],
            lambda a: a.return_at is None and a.rail in ("SPEI", "PIX", "SCT Inst"),
        ):
            a.plant = "late_return_escalate"
            a.entries.append(
                {
                    "kind": "return_late",
                    "offset": rng.randint(1 * DAY, 4 * DAY),
                    "amount": a.amount_dst - rng.randint(100, 5000),
                }
            )
        # H14 consumer ACH unauthorised returns 40-58 days after settlement, found only by the monthly pass
        for a in take(
            hist_settled,
            C["H14"],
            lambda a: (
                a.rail == "ACH"
                and a.return_at is None
                and a.prov_terminal_at + 58 * DAY < ts(2026, 10, 1)
            ),
        ):
            a.plant = "H14"
            a.hard.append("H14")
            a.entries.append(
                {
                    "kind": "return_late",
                    "offset": rng.randint(40 * DAY, 58 * DAY),
                    "amount": a.amount_dst,
                    "reason": "unauthorised_debit",
                }
            )
        # H18 payments created Sunday 2026-10-04 after 20:00 operator time to DE and PH
        # H20 W40 payments settled then returned on Saturday 2026-10-03
        # H21 W40 payments retried across providers; H22 cancelled_by_operator in W40
        self._plant_reporting(rng)

    def _plant_reporting(self, rng: random.Random) -> None:
        C = self.counts
        users_by_platform: dict[str, list[dict[str, Any]]] = {}
        for u in self.users:
            users_by_platform.setdefault(u["platform"], []).append(u)
        pid = len(self.payments)
        life = self.S("lifecycle-plants")
        for k in range(C["H18"]):
            pid += 1
            corridor = "DE" if k % 2 == 0 else "PH"
            h18_amount = None if corridor == "DE" else min(self._amount_usd(rng, "PH"), 500 * 100)
            created = ts(2026, 10, 4, 20, 0, 0) + rng.randint(0, 4 * HOUR - 1)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                h18_amount,
            )
            pm.hard.append("H18")
            self._lifecycle(
                life,
                pm,
                1,
                created,
                None,
                {"rail": "SCT Inst" if corridor == "DE" else "InstaPay", "outcome": "settled"},
            )
        for k in range(C["H20"]):
            pid += 1
            corridor = "MX" if k % 2 == 0 else "BR"
            created = ts(2026, 10, 1, 9, 0, 0) + rng.randint(0, 30 * HOUR)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                None,
            )
            pm.hard.append("H20")
            ret_at = ts(2026, 10, 3, 8, 0, 0) + rng.randint(0, 12 * HOUR)
            a = self._lifecycle(
                life,
                pm,
                1,
                created,
                None,
                {"outcome": "settled", "return": True, "return_offset_s": 0},
            )
            if a.prov_terminal_at is not None:
                a.return_at = ret_at
                a.prov_history = [
                    h for h in a.prov_history if h[1] != raw_status_for(a.provider, "return")
                ]
                a.prov_history.append((ret_at, raw_status_for(a.provider, "return")))
                a.return_reference = a.return_reference or self.ref("RT")
                a.return_amount = a.amount_dst
                a.return_reason = a.return_reason or RETURN_REASONS[0]
        for k in range(C["H21"]):
            pid += 1
            corridor = ["MX", "BR", "PH", "DE", "US"][k % 5]
            created = week_bounds(40)[0] + rng.randint(HOUR, 4 * DAY)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                None,
            )
            pm.hard.append("H21")
            self._lifecycle(
                life,
                pm,
                1,
                created,
                None,
                {
                    "outcome": "failed",
                    "retry": True,
                    "move_provider": True,
                    "retry_outcome": "settled",
                },
            )
        for k in range(C["H22"]):
            pid += 1
            corridor = ["MX", "BR", "PH", "DE", "US"][k % 5]
            created = week_bounds(40)[0] + rng.randint(HOUR, 5 * DAY)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                None,
            )
            pm.hard.append("H22")
            self._lifecycle(
                life,
                pm,
                1,
                created,
                None,
                {"outcome": "cancelled", "reason": "cancelled_by_operator", "retry": False},
            )
        # late cancellations the provider never settled: reported sent, cancelled hours later, with a daily pass in between
        for k in range(C["late_cancel_adopt"]):
            pid += 1
            corridor = "MX" if k % 2 == 0 else "BR"
            d0 = dt.date(2026, 7, 2) + dt.timedelta(days=rng.randint(0, 88))
            created = ts(d0.year, d0.month, d0.day, 20, 0, 0) + rng.randint(0, 3 * HOUR)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(rng, pid, platform, rng.choice(users_by_platform[platform])["user_id"], corridor, created, None)
            a = self._lifecycle(life, pm, 1, created, None, {"outcome": "cancelled", "reason": "cancelled_by_provider", "retry": False})
            if a.prov_terminal_at is None or a.submitted_at is None:
                continue
            lag = self.providers[a.provider].statement_lag_s
            nd = local_date(a.submitted_at, OPERATOR_OFFSET) + dt.timedelta(days=1)
            pass_at = ts(nd.year, nd.month, nd.day, PASS_HOUR, 0, 0)
            cancel_at = pass_at - lag - 1800
            if cancel_at <= a.submitted_at + 600:
                cancel_at = a.submitted_at + 600
            a.prov_terminal_at = cancel_at
            a.prov_history = [h for h in a.prov_history if h[1] != raw_status_for(a.provider, "cancelled")] + [(cancel_at, raw_status_for(a.provider, "cancelled"))]
            a.prov_history.sort()
            a.plant = "late_cancel_adopt"
            a.entries.append({"kind": "late_cancel", "settlement_reference": None, "at": cancel_at})
        # ACH created Friday 2026-10-02 after the last cutoff, submitted Monday 2026-10-05 (H19)
        for _k in range(max(3, C["H22"])):
            pid += 1
            created = ts(2026, 10, 2, 16, 20, 0) + rng.randint(0, 3 * HOUR)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                "US",
                created,
                None,
            )
            pm.hard.append("H19")
            self._lifecycle(life, pm, 1, created, None, {"rail": "ACH", "outcome": "settled"})
        # a burst of payouts on each degraded pair inside its episode, so the trailing-24-hour rule has
        # enough attempts to fire at every world size (the share of failures is the episode's)
        for e in self.episodes:
            for _k in range(110):
                pid += 1
                created = e["start"] + rng.randint(0, max(1, e["end"] - e["start"] - 1))
                platform = rng.choice([p["platform_id"] for p in self.platforms])
                limit = RAIL_LIMIT_MINOR.get(e["rail"])
                amount = None
                if limit is not None:
                    mid = 1.0 if e["corridor"] == "US" else self.src.fixing_on(e["corridor"], created).rate
                    amount = min(self._amount_usd(rng, e["corridor"]), int(limit * 0.7 / mid))
                pm = self._new_payment(rng, pid, platform, rng.choice(users_by_platform[platform])["user_id"], e["corridor"], created, amount)
                pm.hard.append("episode_burst")
                a = self._lifecycle(life, pm, 1, created, None, {"rail": e["rail"], "provider": e["provider"]})
                a.plant = a.plant or "episode_burst"
        # a fill of ordinary wires in the history window, so both Fedwire pairs carry a profile at every seed
        for prov in ("Marigold", "Verdant"):
            for _k in range(90):
                pid += 1
                d0 = dt.date(2026, 6, 29) + dt.timedelta(days=rng.randint(0, 66))
                created = ts(d0.year, d0.month, d0.day, rng.randint(8, 15), rng.randint(0, 59), 0)
                platform = rng.choice([p["platform_id"] for p in self.platforms])
                pm = self._new_payment(rng, pid, platform, rng.choice(users_by_platform[platform])["user_id"], "US", created, rng.randint(60_000, 400_000) * 100)
                pm.hard.append("fedwire_fill")
                self._lifecycle(life, pm, 1, created, None, {"rail": "Fedwire", "provider": prov, "outcome": "settled"})
        # Reserve Banks are open on Friday 2026-07-03: one ACH and one Fedwire payout settle that day
        for rail in ("ACH", "Fedwire"):
            pid += 1
            created = ts(2026, 7, 3, 9, 30, 0)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(rng, pid, platform, rng.choice(users_by_platform[platform])["user_id"], "US", created, 4_000 * 100)
            pm.hard.append("july_3_open")
            a = self._lifecycle(life, pm, 1, created, None, {"rail": rail, "outcome": "settled"})
            a.plant = "july_3_open"
        # the six-figure wire: USD 250,000 Fedwire, submitted 10:15 ET on a banking day in W39, in processing past p95
        pid += 1
        created = ts(2026, 9, 23, 10, 12, 0)
        platform = rng.choice([p["platform_id"] for p in self.platforms])
        pm = self._new_payment(
            rng,
            pid,
            platform,
            rng.choice(users_by_platform[platform])["user_id"],
            "US",
            created,
            250_000 * 100,
        )
        pm.hard.append("six_figure_wire")
        a = self._lifecycle(
            life,
            pm,
            1,
            created,
            None,
            {
                "rail": "Fedwire",
                "provider": "Marigold",
                "outcome": "settled",
                "settle_delay_s": 4 * DAY,
            },
        )
        a.submitted_at = ts(2026, 9, 23, 10, 15, 0)
        a.plant = "six_figure_wire"
        a.hard.append("six_figure_wire")
        # planted stuck set: truly stuck on a 24/7 rail (settles hours late), on a calendar rail (misses windows)
        for k in range(C["stuck_clock"]):
            pid += 1
            corridor = ["MX", "BR", "DE", "PH"][k % 4]
            created = MONITORING_START + rng.randint(DAY, 26 * DAY)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                None,
            )
            pm.hard.append("H16")
            rail = {"MX": "SPEI", "BR": "PIX", "DE": "SCT Inst", "PH": "InstaPay"}[corridor]
            pm.amount_usd_cents = self._amount_under_limit(pm.amount_usd_cents, corridor, rail, created)
            a = self._lifecycle(
                life,
                pm,
                1,
                created,
                None,
                {
                    "rail": rail,
                    "outcome": "settled",
                    "settle_delay_s": rng.randint(4 * HOUR, 30 * HOUR),
                },
            )
            a.plant = "stuck_clock"
            a.hard.append("H16")
        for k in range(C["stuck_calendar"]):
            pid += 1
            corridor = ["US", "PH", "DE"][k % 3]
            created = MONITORING_START + rng.randint(DAY, 24 * DAY)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                None,
            )
            pm.hard.append("H16")
            rail = {"US": "ACH", "PH": "PESONet", "DE": "SCT"}[corridor]
            pm.amount_usd_cents = self._amount_under_limit(pm.amount_usd_cents, corridor, rail, created)
            a = self._lifecycle(
                life,
                pm,
                1,
                created,
                None,
                {
                    "rail": rail,
                    "outcome": "settled",
                    "settle_delay_s": rng.randint(2 * DAY, 4 * DAY),
                },
            )
            a.plant = "stuck_calendar"
            a.hard.append("H16")
        # old but not stuck: Friday 16:30 ET ACH (not stuck Saturday); Friday 18:30 ET Fedwire before Labor Day;
        # PESONet Thursday 2026-08-20 15:00 Manila and Friday 2026-09-25 15:00 Manila (after cutoff); PIX night queue
        specials = [
            ("ACH", "US", ts(2026, 9, 25, 16, 30, 0), "friday_ach", ts(2026, 9, 26, 10, 0, 0)),
            (
                "Fedwire",
                "US",
                ts(2026, 9, 4, 18, 30, 0),
                "after_cutoff_fedwire_labor_day",
                ts(2026, 9, 7, 10, 0, 0),
            ),
            (
                "PESONet",
                "PH",
                ts(2026, 8, 20, 15, 0, 0, offset=ZONE_OFFSET["PH"]),
                "ph_holiday_pesonet",
                ts(2026, 8, 22, 10, 0, 0, offset=ZONE_OFFSET["PH"]),
            ),
            (
                "PESONet",
                "PH",
                ts(2026, 9, 25, 15, 0, 0, offset=ZONE_OFFSET["PH"]),
                "after_cutoff_pesonet",
                ts(2026, 9, 26, 10, 0, 0, offset=ZONE_OFFSET["PH"]),
            ),
            (
                "PIX",
                "BR",
                ts(2026, 9, 22, 22, 30, 0, offset=ZONE_OFFSET["BR"]),
                "pix_night_queue",
                ts(2026, 9, 23, 1, 0, 0, offset=ZONE_OFFSET["BR"]),
            ),
        ]
        for rail, corridor, created, label, as_of in specials:
            pid += 1
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            amount = 30_000 * 100 if rail == "PIX" else None
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                amount,
            )
            pm.hard.append("H16")
            a = self._lifecycle(life, pm, 1, created, None, {"rail": rail, "outcome": "settled"})
            a.plant = "not_stuck:" + label
            a.hard.append("H16")
            a.stuck_as_of = as_of
        # settled seconds before T: ordinary settled attempts in monitoring, as_of = settled + 20 s
        for a in [
            x
            for x in self.attempts
            if x.prov_status == "settled"
            and x.plant is None
            and x.submitted_at is not None
            and x.submitted_at >= MONITORING_START + DAY
        ][: C["settled_before_t"]]:
            a.plant = "settled_before_t"
            a.hard.append("H16")
            a.stuck_as_of = (a.prov_terminal_at or 0) + 20
        # attempts over a rail limit that never reach submitted on that rail: routed to the other rail
        for k in range(C["limit_fail"]):
            pid += 1
            corridor = "PH" if k % 2 == 0 else "DE"
            created = WINDOW_START + rng.randint(DAY, 95 * DAY)
            platform = rng.choice([p["platform_id"] for p in self.platforms])
            mid = self.src.fixing_on(corridor, created).rate
            limit = RAIL_LIMIT_MINOR["InstaPay" if corridor == "PH" else "SCT Inst"]
            assert limit is not None
            usd = int((limit * 1.3) / mid)
            pm = self._new_payment(
                rng,
                pid,
                platform,
                rng.choice(users_by_platform[platform])["user_id"],
                corridor,
                created,
                usd,
            )
            pm.hard.append("limit_routed")
            a = self._lifecycle(life, pm, 1, created, None, {"outcome": "settled"})
            a.plant = "limit_routed"
        self.payments.sort(key=lambda p: (p.created_at, p.payment_id))

    # ---- 5. provider events, deliveries, statements, our ledger ----
    def build_events_and_ledger(self) -> None:
        rng = self.S("events")
        for a in sorted(self.attempts, key=lambda x: x.attempt_id):
            if a.deleted:
                continue
            if a.submitted_at is None:
                a.timeline = [(a.created_at, "created", "system", None, None)]
                continue
            p = self.providers[a.provider]
            # the provider's event stream
            deliveries: list[tuple[int, Event]] = []
            prov_events: list[tuple[int, str]] = list(a.prov_history)
            # statement-only facts (late cancel, late failure flip) are also provider events
            for e in a.entries:
                if e.get("kind") == "late_cancel" and a.prov_status != "cancelled":
                    prov_events.append((e["at"], raw_status_for(a.provider, "cancelled")))
                if e.get("kind") == "status" and "at" in e:
                    prov_events.append(
                        (e["at"], e["failure_code"] and raw_status_for(a.provider, "failed"))
                    )
                if e.get("kind") == "return_late":
                    pass  # a late return is a statement fact the provider never pushed
            prov_events.sort()
            dropped_terminal = a.plant == "H8" or a.plant == "stuck_clock"
            for idx, (t, raw) in enumerate(prov_events):
                unified = self._unify(a.provider, raw)
                self._event_counter += 1
                ev = Event(
                    event_id=f"evt_{self._event_counter:08d}",
                    attempt_id=a.attempt_id,
                    provider=a.provider,
                    provider_event_id=f"{a.provider[:2].upper()}E{self._event_counter:08d}",
                    occurred_at=t,
                    raw_status=raw,
                    unified=unified,
                    source="webhook",
                    delivered_at=None,
                    signature_valid=True,
                    applied=False,
                    apply_result="pending",
                    reference=None,
                    failure_code=None,
                    payload={},
                )
                if unified == "submitted":
                    ev.reference = a.provider_reference
                    ev.source = "api"
                    ev.delivered_at = t
                    deliveries.append((t, ev))
                    continue
                if unified == "settled":
                    ev.reference = a.settlement_reference
                if unified == "failed":
                    ev.failure_code = a.raw_failure
                if unified == "return":
                    ev.reference = a.return_reference
                # delivery
                silent = self.webhook_silent(a.provider, t)
                late_cancel_plant = (
                    any(e.get("kind") == "late_cancel" for e in a.entries)
                    and unified == "cancelled"
                )
                stale_plant = a.plant == "status_requery" and unified == "failed"
                late_fail_plant = (
                    any(e.get("kind") == "status" and "at" in e for e in a.entries)
                    and unified == "failed"
                    and idx == len(prov_events) - 1
                )
                if (
                    silent is not None
                    or late_cancel_plant
                    or late_fail_plant
                    or stale_plant
                    or (dropped_terminal and unified in TERMINAL)
                    or (a.plant in ("missing_at_requery",) and unified in TERMINAL)
                ):
                    ev.apply_result = "dropped"
                    ev.delivered_at = None
                    ev.source = "webhook"
                    if silent is not None:
                        ev.payload["incident"] = silent["incident_id"]
                    deliveries.append((10**12, ev))
                    continue
                if rng.random() < p.webhook_drop and unified in TERMINAL:
                    ev.apply_result = "dropped"
                    deliveries.append((10**12, ev))
                    continue
                lat = rng.randint(1, 30)
                if rng.random() < p.webhook_late:
                    lat = rng.randint(HOUR, 72 * HOUR)
                ev.delivered_at = t + lat
                deliveries.append((ev.delivered_at, ev))
                if False:
                    self._event_counter += 1
                    dup = Event(
                        **{
                            **ev.__dict__,
                            "event_id": f"evt_{self._event_counter:08d}",
                            "delivered_at": ev.delivered_at + rng.randint(10, 600),
                            "payload": {"duplicate_of": ev.event_id},
                        }
                    )
                    deliveries.append((dup.delivered_at, dup))
                if rng.random() < p.webhook_invalid_sig:
                    self._event_counter += 1
                    bad = Event(
                        **{
                            **ev.__dict__,
                            "event_id": f"evt_{self._event_counter:08d}",
                            "signature_valid": False,
                            "delivered_at": ev.delivered_at + rng.randint(1, 300),
                            "payload": {"forged_copy_of": ev.event_id},
                        }
                    )
                    deliveries.append((bad.delivered_at or 0, bad))
            # out-of-order: a terminal delivered before its processing
            if rng.random() < p.webhook_out_of_order or a.plant == "H6":
                proc = [
                    d
                    for d in deliveries
                    if d[1].unified == "processing"
                    and d[1].delivered_at is not None
                    and "duplicate_of" not in d[1].payload
                    and d[1].signature_valid
                ]
                term = [
                    d
                    for d in deliveries
                    if d[1].unified in TERMINAL
                    and d[1].delivered_at is not None
                    and d[1].signature_valid
                    and "duplicate_of" not in d[1].payload
                ]
                if proc and term:
                    pe = proc[0][1]
                    te = term[0][1]
                    assert te.delivered_at is not None
                    pe.delivered_at = te.delivered_at + rng.randint(5, 120)
                    pe.payload["out_of_order"] = True
                    deliveries = [
                        (d[1].delivered_at if d[1].delivered_at is not None else 10**12, d[1])
                        for d in deliveries
                    ]
                    if "H6" not in a.hard:
                        a.hard.append("H6")
            extra: list[tuple[int, Event]] = []
            for _d, ev in deliveries:
                if ev.delivered_at is None or ev.source != "webhook" or not ev.signature_valid or ev.payload.get("duplicate_of"):
                    continue
                if rng.random() < p.webhook_duplicate:
                    self._event_counter += 1
                    dup = Event(**{**ev.__dict__, "event_id": f"evt_{self._event_counter:08d}",
                                   "delivered_at": ev.delivered_at + rng.randint(10, 600), "payload": {"duplicate_of": ev.event_id}})
                    extra.append((dup.delivered_at or 0, dup))
                if rng.random() < p.webhook_invalid_sig:
                    self._event_counter += 1
                    bad = Event(**{**ev.__dict__, "event_id": f"evt_{self._event_counter:08d}", "signature_valid": False,
                                   "delivered_at": ev.delivered_at + rng.randint(1, 300), "payload": {"forged_copy_of": ev.event_id}})
                    extra.append((bad.delivered_at or 0, bad))
            deliveries.extend(extra)
            deliveries.sort(key=lambda d: (d[0], d[1].event_id))
            # apply to our ledger
            timeline: list[tuple[int, str, str, str | None, str | None]] = [
                (a.created_at, "created", "system", None, None)
            ]
            seen_ids: set[str] = set()
            rank = {
                s: i
                for i, s in enumerate(
                    ("created", "submitted", "processing", "settled", "failed", "cancelled")
                )
            }
            cur = "created"
            ret_applied: int | None = None
            for _, ev in deliveries:
                if ev.delivered_at is None:
                    continue
                if not ev.signature_valid:
                    ev.apply_result = "invalid_signature"
                    continue
                if ev.provider_event_id in seen_ids:
                    ev.apply_result = "duplicate"
                    continue
                seen_ids.add(ev.provider_event_id)
                if ev.unified == "return":
                    if cur == "settled" and ret_applied is None:
                        ret_applied = ev.delivered_at
                        ev.applied = True
                        ev.apply_result = "applied"
                    else:
                        ev.apply_result = "ignored_no_settlement"
                    continue
                if cur in TERMINAL:
                    ev.apply_result = "terminal_already"
                    continue
                if rank[ev.unified] <= rank[cur]:
                    ev.apply_result = "out_of_order_superseded"
                    continue
                cur = ev.unified
                ev.applied = True
                ev.apply_result = "applied"
                timeline.append((ev.delivered_at, cur, ev.source, ev.reference, ev.failure_code))
            a.timeline = timeline
            a.ledger_return_at = ret_applied
            if ret_applied is not None:
                self._event_counter += 1
                deliveries.append((ret_applied + 60, Event(
                    event_id=f"evt_{self._event_counter:08d}", attempt_id=a.attempt_id, provider=a.provider,
                    provider_event_id="", occurred_at=ret_applied + 60, raw_status="refund", unified="refund", source="system",
                    delivered_at=ret_applied + 60, signature_valid=True, applied=True, apply_result="applied",
                    reference=a.return_reference, failure_code=None, payload={"refund_for_return": True})))
            a.status_source = "webhook"
            self.events.extend(ev for _, ev in deliveries)
            # polls for a dropped terminal: at submitted + 24 h (24/7) or + 2 opportunities (calendar)
            self._poll(rng, a)
            # statement entries (the provider's view, after its lag)
            self._statement(rng, a)
            # a retry exists only once our ledger learned the failure
            nxt = self.attempt_by_id.get(f"att_{a.payment_id[4:]}_{a.n + 1}")
            if nxt is not None:
                learned, st = self.terminal_of(a)
                if learned is None or st not in ("failed", "cancelled"):
                    self._delete_subtree(nxt)
                elif nxt.created_at < learned:
                    self._shift_attempt(nxt, learned + rng.randint(120, 6 * HOUR) - nxt.created_at)

    def _supersede_after(self, a: Attempt, at: int) -> None:
        """Once the ledger holds a terminal status from time `at`, later provider-sourced status
        deliveries are recorded as superseded, never applied."""
        for ev in self.events:
            if (
                ev.attempt_id == a.attempt_id
                and ev.source in ("poll", "webhook")
                and ev.applied
                and ev.unified in ("submitted", "processing", "settled", "failed", "cancelled")
                and ev.delivered_at is not None
                and ev.delivered_at > at
            ):
                ev.applied = False
                ev.apply_result = "terminal_already"
        a.timeline.sort(key=lambda e: e[0])

    def _shift_attempt(self, a: Attempt, delta: int) -> None:
        """Move an attempt later by `delta` seconds: the creation moves, the submission follows the
        rail's own rule from the new creation (a hold is over by then), the provider's events keep
        their distance from the submission on a 24/7 rail and follow the rail's clock on a
        calendar rail. What falls past T0 is still open."""
        srng = self.S("shift")
        a.created_at += delta
        a.hold_until = None
        if a.submitted_at is None:
            nxt = self.attempt_by_id.get(f"att_{a.payment_id[4:]}_{a.n + 1}")
            if nxt is not None:
                self._shift_attempt(nxt, delta)
            return
        old_submitted = a.submitted_at
        a.submitted_at = self.submission_time(a, srng)
        shift = a.submitted_at - old_submitted
        if a.rail in CALENDAR_RAILS and a.prov_status == "settled" and a.prov_terminal_at is not None:
            if a.rail == "Fedwire":
                a.prov_terminal_at = fedwire_settlement(a.submitted_at, self.cal[a.corridor], srng.randint(5, 90))
            else:
                a.prov_terminal_at = settlement_time(a.rail, a.submitted_at, self.cal[a.corridor]) + srng.randint(0, 300)
        elif a.prov_terminal_at is not None:
            a.prov_terminal_at += shift
        if a.return_at is not None:
            a.return_at += shift
        hist = []
        for h, s in a.prov_history:
            if s == raw_status_for(a.provider, "submitted"):
                hist.append((a.submitted_at, s))
            elif s == raw_status_for(a.provider, "processing"):
                hist.append((a.submitted_at + srng.randint(1, 90), s))
            elif s in (raw_status_for(a.provider, "settled"), raw_status_for(a.provider, "failed"), raw_status_for(a.provider, "cancelled")) and a.prov_terminal_at is not None:
                hist.append((a.prov_terminal_at, s))
            elif s == raw_status_for(a.provider, "return") and a.return_at is not None:
                hist.append((a.return_at, s))
            else:
                hist.append((h + shift, s))
        a.prov_history = sorted(hist)
        for e in a.entries:
            for k in ("at", "until", "as_of"):
                if k in e:
                    e[k] += shift
        if a.stuck_as_of is not None:
            a.stuck_as_of += shift
        mid, markup, q = self.quote(a.provider, a.corridor, a.created_at)
        a.fx_mid, a.fx_markup, a.fx_quote = mid, markup, q
        a.amount_dst = int(round(a.amount_usd_cents * q))
        a.fee_minor = self.fee(a.provider, a.corridor, a.rail, a.amount_dst)
        if a.submitted_at > T0:
            a.submitted_at = None
            a.prov_status = None
            a.prov_terminal_at = None
            a.return_at = None
            a.prov_history = []
        elif a.prov_terminal_at is not None and a.prov_terminal_at > T0:
            a.prov_status = None
            a.prov_terminal_at = None
            a.return_at = None
            a.prov_history = [h for h in a.prov_history if h[0] <= T0]
        elif a.return_at is not None and a.return_at > T0:
            a.return_at = None
            a.prov_history = [h for h in a.prov_history if h[0] <= T0]
        nxt = self.attempt_by_id.get(f"att_{a.payment_id[4:]}_{a.n + 1}")
        if nxt is not None:
            self._shift_attempt(nxt, delta + shift)

    def _delete_subtree(self, a: Attempt) -> None:
        a.deleted = True
        pm = self.payment_by_id_map()[a.payment_id]
        pm.attempts = [x for x in pm.attempts if x is not a]
        self.attempts = [x for x in self.attempts if x is not a]
        self.attempt_by_id.pop(a.attempt_id, None)
        nxt = self.attempt_by_id.get(f"att_{a.payment_id[4:]}_{a.n + 1}")
        if nxt is not None:
            self._delete_subtree(nxt)

    def payment_by_id_map(self) -> dict[str, Payment]:
        m = getattr(self, "_pm_map", None)
        if m is None or len(m) != len(self.payments):
            m = {pm.payment_id: pm for pm in self.payments}
            self._pm_map = m
        return m

    def _unify(self, provider: str, raw: str) -> str:
        from .providers import STATUS_VOCABULARY

        return STATUS_VOCABULARY[provider][raw]

    def _poll(self, rng: random.Random, a: Attempt) -> None:
        cur = a.timeline[-1][1]
        if cur in TERMINAL or a.submitted_at is None:
            return
        if a.prov_status is None:
            return  # still open at the provider too
        cal = self.cal[a.corridor]
        poll_at: int
        if a.rail in CLOCK_RAILS:
            poll_at = a.submitted_at + POLL_DELAY_CLOCK_S
        else:
            nc = next_cutoff(a.rail, a.submitted_at + 1, cal)
            for _ in range(POLL_DELAY_OPPORTUNITIES - 1):
                assert nc is not None
                nc = next_cutoff(a.rail, nc + 1, cal)
            assert nc is not None
            poll_at = nc + 600
        if poll_at > T0:
            return
        assert a.prov_terminal_at is not None
        self._event_counter += 1
        ok = (
            rng.random() < 0.8
            and a.plant != "missing_at_requery"
            and not (a.plant == "stuck_clock" and rng.random() < 0.5)
        )
        if a.plant in ("H8", "late_cancel_adopt"):
            ok = True
        if ok and poll_at < a.prov_terminal_at:
            self.events.append(
                Event(
                    event_id=f"evt_{self._event_counter:08d}", attempt_id=a.attempt_id, provider=a.provider,
                    provider_event_id=f"POLL{self._event_counter:08d}", occurred_at=poll_at,
                    raw_status=raw_status_for(a.provider, "processing"), unified="processing", source="poll",
                    delivered_at=poll_at, signature_valid=True, applied=False, apply_result="poll_in_flight",
                    reference=None, failure_code=None, payload={},
                )
            )
            return
        if ok and poll_at >= a.prov_terminal_at:
            status = a.prov_status
            ref = a.settlement_reference if status == "settled" else None
            code = a.raw_failure if status == "failed" else None
            self.events.append(
                Event(
                    event_id=f"evt_{self._event_counter:08d}",
                    attempt_id=a.attempt_id,
                    provider=a.provider,
                    provider_event_id=f"POLL{self._event_counter:08d}",
                    occurred_at=poll_at,
                    raw_status=raw_status_for(a.provider, status),
                    unified=status,
                    source="poll",
                    delivered_at=poll_at,
                    signature_valid=True,
                    applied=True,
                    apply_result="applied",
                    reference=ref,
                    failure_code=code,
                    payload={},
                )
            )
            a.timeline.append((poll_at, status, "poll", ref, code))
            a.timeline.sort(key=lambda e: e[0])
            a.status_source = "poll"
            self._supersede_after(a, poll_at)
            if (
                a.return_at is not None
                and a.return_at <= poll_at
                and status == "settled"
                and a.ledger_return_at is None
            ):
                a.ledger_return_at = poll_at
                self._event_counter += 1
                self.events.append(Event(
                    event_id=f"evt_{self._event_counter:08d}", attempt_id=a.attempt_id, provider=a.provider,
                    provider_event_id="", occurred_at=poll_at + 60, raw_status="refund", unified="refund", source="system",
                    delivered_at=poll_at + 60, signature_valid=True, applied=True, apply_result="applied",
                    reference=a.return_reference, failure_code=None, payload={"refund_for_return": True}))
        else:
            # the poll failed: keep the last reported status; 48 h after submission assume failed
            self.events.append(
                Event(
                    event_id=f"evt_{self._event_counter:08d}",
                    attempt_id=a.attempt_id,
                    provider=a.provider,
                    provider_event_id=f"POLL{self._event_counter:08d}",
                    occurred_at=poll_at,
                    raw_status="",
                    unified=cur,
                    source="poll",
                    delivered_at=poll_at,
                    signature_valid=True,
                    applied=False,
                    apply_result="poll_failed",
                    reference=None,
                    failure_code=None,
                    payload={},
                )
            )
            a.status_source = "timeout_assumed"
            a.poll_failed_at = poll_at

    def _statement(self, rng: random.Random, a: Attempt) -> None:
        """The provider's statement entries for the attempt: the provider's view after its lag.
        Only terminal entries appear (plus the plants)."""
        if a.prov_status is None or a.prov_terminal_at is None:
            return
        p = self.providers[a.provider]
        lag = p.statement_lag_s
        entry_at = a.prov_terminal_at + lag + rng.randint(0, 600)
        amount = a.amount_dst
        status = a.prov_status
        reference: str | None = a.settlement_reference if status == "settled" else None
        failure_code: str | None = a.raw_failure if status in ("failed", "cancelled") else None
        stmt: list[dict[str, Any]] = []
        plant_entries = list(a.entries)
        missing_until = None
        for e in plant_entries:
            k = e.get("kind")
            if k == "amount":
                amount = a.amount_dst - e["diff"] if "diff" in e else e["amount"]
            if k == "no_reference":
                reference = None
            if k == "missing_until":
                missing_until = e["until"]
        if a.plant == "H5":
            status = "settled"
            reference = [e for e in plant_entries if e.get("kind") == "status"][0]["reference"]
            failure_code = None
        if a.plant == "status_requery":
            # a stale entry: the provider recorded failed before it settled, never pushed that
            # failure, and its statement still shows the failed entry without final fields
            stale_event_at = [e for e in plant_entries if e.get("kind") == "status_stale"][0]["event_at"]
            stmt.append(
                {
                    "entry_at": stale_event_at + lag,
                    "status": "failed",
                    "amount": amount,
                    "fee": a.fee_minor,
                    "reference": None,
                    "failure_code": None,
                    "entry_id": self.ref("SE"),
                }
            )
            a.statement_entries_final = stmt  # type: ignore[attr-defined]
            a.entries = [{"kind": "stmt", "rows": stmt}]
            return
        if missing_until is not None:
            entry_at = missing_until
        assert a.submitted_at is not None
        if missing_until is None:
            stmt.append({"entry_at": a.submitted_at + lag, "status": "processing", "amount": amount,
                         "fee": a.fee_minor, "reference": None, "failure_code": None, "entry_id": self.ref("SE")})
            entry_at = max(entry_at, a.submitted_at + lag + 1)
        if a.plant == "late_cancel_adopt":
            # the provider reported sent, then cancelled: no terminal entry of its own
            for e in plant_entries:
                if e.get("kind") == "late_cancel":
                    stmt.append({"entry_at": e["at"] + lag, "status": "cancelled", "amount": amount, "fee": a.fee_minor,
                                 "reference": None, "failure_code": None, "entry_id": self.ref("SE"),
                                 "settlement_reference": None, "previously": "processing"})
            stmt.sort(key=lambda s: s["entry_at"])
            a.entries = [{"kind": "stmt", "rows": stmt}]
            return
        stmt.append(
            {
                "entry_at": entry_at,
                "status": status,
                "amount": amount,
                "fee": a.fee_minor,
                "reference": reference,
                "failure_code": failure_code,
                "entry_id": self.ref("SE"),
            }
        )
        for e in plant_entries:
            k = e.get("kind")
            if k == "late_cancel":
                stmt.append(
                    {
                        "entry_at": e["at"] + lag,
                        "status": "cancelled",
                        "amount": amount,
                        "fee": a.fee_minor,
                        "reference": None,
                        "failure_code": None,
                        "entry_id": self.ref("SE"),
                        "settlement_reference": e["settlement_reference"],
                        "previously": "settled" if a.prov_status == "settled" else "processing",
                    }
                )
            if k == "status" and "at" in e:
                stmt.append(
                    {
                        "entry_at": e["at"] + lag,
                        "status": "failed",
                        "amount": amount,
                        "fee": a.fee_minor,
                        "reference": None,
                        "failure_code": e["failure_code"],
                        "entry_id": self.ref("SE"),
                    }
                )
            if k == "amount_correction":
                stmt.append({"entry_at": entry_at + e["offset"], "status": status, "amount": a.amount_dst - e["diff"], "fee": a.fee_minor,
                             "reference": reference, "failure_code": None, "entry_id": self.ref("SE")})
            if k == "duplicate":
                stmt.append(
                    {
                        "entry_at": entry_at + rng.randint(HOUR, 2 * DAY),
                        "status": "settled",
                        "amount": e["second_amount"],
                        "fee": 0,
                        "reference": e["second_reference"],
                        "failure_code": None,
                        "entry_id": self.ref("SE"),
                        "duplicate": True,
                    }
                )
            if k == "return_late":
                t = a.prov_terminal_at + e["offset"]
                if t <= T0:
                    stmt.append(
                        {
                            "entry_at": t + lag,
                            "status": "settled",
                            "amount": amount,
                            "fee": a.fee_minor,
                            "reference": reference,
                            "failure_code": None,
                            "entry_id": self.ref("SE"),
                            "return_reference": self.ref("RT"),
                            "return_amount": e["amount"],
                            "return_at": t,
                            "return_reason": e.get("reason", "beneficiary_refused"),
                        }
                    )
        if a.return_at is not None and not any("return_reference" in s for s in stmt):
            stmt.append(
                {
                    "entry_at": a.return_at + lag,
                    "status": "settled",
                    "amount": amount,
                    "fee": a.fee_minor,
                    "reference": reference,
                    "failure_code": None,
                    "entry_id": self.ref("SE"),
                    "return_reference": a.return_reference,
                    "return_amount": a.return_amount,
                    "return_at": a.return_at,
                    "return_reason": a.return_reason,
                }
            )
        stmt.sort(key=lambda s: s["entry_at"])
        a.entries = [{"kind": "stmt", "rows": stmt}]

    def orphans(self) -> None:
        """Statement entries with no attempt of ours (missing_in_ledger)."""
        rng = self.S("orphans")
        for a in self.attempts:
            if a.plant in ("missing_in_create", "missing_in_escalate"):
                rows = a.entries[0]["rows"]
                assert a.prov_terminal_at is not None
                if a.plant == "missing_in_create":
                    # our row stayed created with no provider reference; the provider has it under our client reference
                    a.timeline = [(a.created_at, "created", "system", None, None)]
                    a.provider_reference = None
                    a.status_source = "webhook"
                    a.ledger_return_at = None
                    a.poll_failed_at = None
                    self.events = [e for e in self.events if e.attempt_id != a.attempt_id]
                    client_ref = a.client_reference
                else:
                    client_ref = "CR" + str(rng.randint(10**7, 10**8))
                    a.plant = "missing_in_escalate_source"
                    a.entries = [{"kind": "stmt", "rows": rows}]
                self.orphan_entries.append(
                    {
                        "entry_id": self.ref("SE"),
                        "provider": a.provider,
                        "client_reference": client_ref,
                        "entry_at": rows[0]["entry_at"],
                        "status": a.prov_status,
                        "amount": a.amount_dst,
                        "fee": a.fee_minor,
                        "reference": self.ref("ST"),
                        "source_attempt": a.attempt_id if a.plant == "missing_in_create" else None,
                        "plant": "missing_in_create"
                        if a.plant == "missing_in_create"
                        else "missing_in_escalate",
                    }
                )
                if a.plant == "missing_in_create":
                    a.entries = [{"kind": "stmt", "rows": []}]

    # ---- 6. profiles, baselines, degradation ----
    def ledger_status_at(self, a: Attempt, t: int) -> tuple[str, str, int, str | None, str | None]:
        """(status, source, last_event_at, reference, failure_code) of our ledger at `t`.

        A terminal status set from a webhook or a poll is never overwritten; an operator may
        correct a status that was only assumed. `last_event_at` is the time of the last applied
        provider-sourced or operator event. When the poll failed and the attempt is still not
        terminal 48 hours after submission, the ledger assumes failed (status source
        timeout_assumed); the assumption is not an event.
        """
        status, source, last_at, ref, code = "created", "system", a.created_at, None, None
        for ev in a.timeline:
            if ev[0] > t:
                break
            if ev[2] == "system":
                continue
            if status in TERMINAL and ev[2] != "operator":
                continue
            status, source, last_at = ev[1], ev[2], ev[0]
            if ev[3] is not None:
                ref = ev[3]
            code = ev[4]
        if (
            status not in TERMINAL
            and a.poll_failed_at is not None
            and a.poll_failed_at <= t
            and a.submitted_at is not None
            and t >= a.submitted_at + ASSUME_FAILED_AFTER_S
        ):
            return "failed", "timeout_assumed", last_at, ref, "assumed_failed_no_answer"
        return status, source, last_at, ref, code

    def assumed_failed_from(self, a: Attempt) -> int | None:
        """When the ledger starts assuming an attempt failed, if its poll failed: 48 hours after
        submission or the failed poll, whichever is later (`ledger_status_at`'s rule). On a
        calendar rail the poll waits two settlement opportunities, which can fall after the 48
        hours (a Friday payout polled on Monday)."""
        if a.poll_failed_at is None or a.submitted_at is None:
            return None
        return max(a.submitted_at + ASSUME_FAILED_AFTER_S, a.poll_failed_at)

    def terminal_of(self, a: Attempt) -> tuple[int | None, str | None]:
        """When our ledger first reached the terminal status it holds at T0, and that status."""
        final = self.ledger_status_at(a, T0)
        if final[0] not in TERMINAL:
            return None, None
        assumed = self.assumed_failed_from(a)
        times = sorted(
            {ev[0] for ev in a.timeline}
            | ({a.submitted_at + ASSUME_FAILED_AFTER_S} if a.submitted_at is not None else set())
            | ({assumed} if assumed is not None else set())
        )
        for tt in times:
            if tt > T0:
                break
            st = self.ledger_status_at(a, tt)
            if st[0] == final[0] and st[1] == final[1]:
                return tt, final[0]
        return T0, final[0]

    def build_profiles(self) -> None:
        samples: dict[tuple[str, str, str], list[int]] = {}
        counts24: dict[tuple[str, str, str], list[tuple[int, bool]]] = {}
        for a in self.attempts:
            if a.submitted_at is None:
                continue
            key = (a.provider, a.corridor, a.rail)
            counts24.setdefault(key, []).append((a.submitted_at, self.ledger_status_at(a, T0)[0] == "failed"))
            if (
                a.prov_status == "settled"
                and a.prov_terminal_at is not None
                and a.submitted_at < HISTORY_END
                and self.ledger_status_at(a, T0)[0] == "settled"
            ):
                samples.setdefault(key, []).append(
                    elapsed_on_clock(
                        a.rail, a.submitted_at, a.prov_terminal_at, self.cal[a.corridor]
                    )
                )
        for name in PROVIDER_NAMES:
            for corridor, rail in COVERAGE[name]:
                key = (name, corridor, rail)
                vals = samples.get(key, [])
                hist = [
                    x
                    for x in counts24.get(key, [])
                    if x[0] < HISTORY_END and self.episode_at(name, corridor, rail, x[0]) is None
                ]
                fails = sum(1 for _, f in hist if f)
                self.baseline[key] = round(fails / len(hist), 4) if hist else 0.0
                if len(vals) >= self.profile_min_count:
                    self.profiles[key] = {
                        "count": len(vals),
                        "p50": percentile(vals, 0.5),
                        "p95": percentile(vals, 0.95),
                        "unit": clock_unit(rail),
                    }
                else:
                    self.profiles[key] = {
                        "count": len(vals),
                        "p50": None,
                        "p95": None,
                        "unit": clock_unit(rail),
                    }
        self._counts24 = {k: sorted(v) for k, v in counts24.items()}

    def trailing(self, key: tuple[str, str, str], t: int) -> tuple[int, int]:
        rows = self._counts24.get(key, [])
        n = f = 0
        for s, failed in rows:
            if t - 24 * HOUR <= s < t:
                n += 1
                if failed:
                    f += 1
        return n, f

    def degraded_at(self, key: tuple[str, str, str], t: int) -> bool:
        n, f = self.trailing(key, t)
        return evaluate_degraded(n, f, self.baseline[key])

    def build_degradation(self) -> None:
        for key in self.baseline:
            hours: list[int] = []
            t = WINDOW_START
            while t <= T0:
                if self.degraded_at(key, t):
                    hours.append(t)
                t += HOUR
            self.degraded_hours[key] = hours
        # incident records: first observed hour to the first hour it stops
        for e in self.episodes:
            key = (e["provider"], e["corridor"], e["rail"])
            hs = [h for h in self.degraded_hours[key] if e["start"] <= h <= e["end"] + 24 * HOUR]
            first = hs[0] if hs else None
            last = hs[-1] if hs else None
            self.incidents.append(
                {
                    "incident_id": f"inc_{len(self.incidents) + 1:02d}",
                    "provider": e["provider"],
                    "service": "payouts",
                    "corridor": e["corridor"],
                    "rail": e["rail"],
                    "first_observed": first,
                    "resolved_at": (last + HOUR) if last is not None else None,
                    "episode_id": e["episode_id"],
                }
            )
        for w in self.webhook_incidents:
            self.incidents.append(
                {
                    "incident_id": f"inc_{len(self.incidents) + 1:02d}",
                    "provider": w["provider"],
                    "service": "webhooks",
                    "corridor": None,
                    "rail": None,
                    "first_observed": w["start"],
                    "resolved_at": w["end"],
                    "episode_id": w["incident_id"],
                }
            )

    # ---- 7. passes and breaks ----
    def statement_rows_at(self, a: Attempt, t: int) -> list[dict[str, Any]]:
        if not a.entries or a.entries[0].get("kind") != "stmt":
            return []
        return [r for r in a.entries[0]["rows"] if r["entry_at"] <= t]

    def build_passes(self) -> None:
        rng = self.S("passes")
        schedule: list[tuple[int, str, int]] = []
        d = dt.date(2026, 6, 29)
        while d <= dt.date(2026, 10, 5):
            at = ts(d.year, d.month, d.day, PASS_HOUR, 0, 0)
            if at - 7 * DAY >= WINDOW_START:
                schedule.append((at, "daily", 7))
            if d.weekday() == 0 and at - 30 * DAY >= WINDOW_START:
                schedule.append((at, "weekly", 30))
            if d.day == 1 and at - 90 * DAY >= WINDOW_START:
                schedule.append((at, "monthly", 90))
            d = d + dt.timedelta(days=1)
        schedule.sort(key=lambda s: (s[0], {"daily": 0, "weekly": 1, "monthly": 2}[s[1]]))
        by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
        latest_for: dict[tuple[str, str], dict[str, Any]] = {}
        attempts_sorted = sorted(
            (a for a in self.attempts if a.submitted_at is not None),
            key=lambda a: a.submitted_at or 0,
        )
        sub_times = [a.submitted_at or 0 for a in attempts_sorted]
        import bisect

        for pi, (D, cadence, L) in enumerate(schedule):
            pass_id = f"pass_{pi + 1:04d}"
            lo = bisect.bisect_left(sub_times, D - L * DAY)
            hi = bisect.bisect_left(sub_times, D)
            compared = attempts_sorted[lo:hi]
            raised = 0
            reobserved = 0
            disagreeing: set[str] = set()
            for a in compared:
                findings = self.compare(a, D)
                for kind, ev, ident in findings:
                    disagreeing.add(a.attempt_id)
                    key = (a.attempt_id, kind, ident)
                    existing = by_identity.get(key)
                    if existing is not None:
                        if existing["state"] in ("open", "escalated"):
                            existing["seen_in_passes"].append(pass_id)
                            reobserved += 1
                        continue
                    prev = latest_for.get((a.attempt_id, kind))
                    version = 1
                    if prev is not None:
                        prev["superseded"] = True
                        version = prev["version"] + 1
                    br: dict[str, Any] = {
                        "break_id": f"brk_{len(self.breaks) + 1:06d}",
                        "attempt_id": a.attempt_id,
                        "kind": kind,
                        "evidence": ev,
                        "evidence_digest": ident,
                        "version": version,
                        "superseded": False,
                        "found_at": D,
                        "first_pass": pass_id,
                        "seen_in_passes": [pass_id],
                        "state": "open",
                        "resolved_at": None,
                        "resolution": None,
                        "hard": list(a.hard) if a.plant else [],
                        "plant": a.plant,
                        "correct_resolution": correct_resolution(kind, ev),
                        "as_of": D,
                    }
                    self.breaks.append(br)
                    by_identity[key] = br
                    latest_for[(a.attempt_id, kind)] = br
                    raised += 1
            # orphan entries on this provider's statement as of D
            for o in self.orphan_entries:
                if o["entry_at"] <= D and o["entry_at"] >= D - L * DAY:
                    match = None
                    if o["source_attempt"] is not None:
                        src = self.attempt_by_id[o["source_attempt"]]
                        st = self.ledger_status_at(src, D)
                        if st[0] == "created" and src.provider_reference is None:
                            match = src.attempt_id
                    ev = {
                        "provider": o["provider"],
                        "statement_client_reference": o["client_reference"],
                        "statement_reference": o["reference"],
                        "statement_status": o["status"],
                        "statement_amount": o["amount"],
                        "matching_attempt": match,
                        "as_of": D,
                    }
                    ident = _digest({k: v for k, v in ev.items() if k != "as_of"})
                    key = (o["entry_id"], "missing_in_ledger", ident)
                    existing = by_identity.get(key)
                    if existing is not None:
                        if existing["state"] in ("open", "escalated"):
                            existing["seen_in_passes"].append(pass_id)
                            reobserved += 1
                        continue
                    br = {
                        "break_id": f"brk_{len(self.breaks) + 1:06d}",
                        "attempt_id": o["source_attempt"],
                        "kind": "missing_in_ledger",
                        "evidence": ev,
                        "evidence_digest": ident,
                        "version": 1,
                        "superseded": False,
                        "found_at": D,
                        "first_pass": pass_id,
                        "seen_in_passes": [pass_id],
                        "state": "open",
                        "resolved_at": None,
                        "resolution": None,
                        "hard": [],
                        "plant": o["plant"],
                        "statement_entry_id": o["entry_id"],
                        "correct_resolution": correct_resolution("missing_in_ledger", ev),
                        "as_of": D,
                    }
                    self.breaks.append(br)
                    by_identity[key] = br
                    raised += 1
            n_compared = len(compared)
            n_matched = n_compared - len(disagreeing)
            self.passes.append(
                {
                    "pass_id": pass_id,
                    "cadence": cadence,
                    "run_at": D,
                    "lookback_days": L,
                    "compared": n_compared,
                    "matched": n_matched,
                    "match_rate": round(n_matched / n_compared, 5) if n_compared else None,
                    "raised": raised,
                    "reobserved": reobserved,
                }
            )
            # resolve history-window breaks found at this pass; clear transient ones no longer seen
            for br in self.breaks:
                if br["state"] == "open" and br["found_at"] == D:
                    self._resolve(rng, br, D)
            for br in self.breaks:
                if (
                    br["state"] == "open"
                    and br["found_at"] < D
                    and pass_id not in br["seen_in_passes"]
                    and br["kind"] in ("missing_at_provider", "ours_behind")
                    and br["found_at"] >= D - L * DAY
                    and cadence == "daily"
                ):
                    attempt_id = str(br["attempt_id"]) if br["attempt_id"] else ""
                    if not any(
                        f[0] == br["kind"]
                        for f in (
                            self.compare(self.attempt_by_id[attempt_id], D)
                            if attempt_id
                            else []
                        )
                    ):
                        br["state"] = "cleared"
                        br["cleared_at"] = D

    def compare(self, a: Attempt, D: int) -> list[tuple[str, dict[str, Any], str]]:
        """Disagreements between our ledger and the provider's statement for `a` as of `D`."""
        ours_status, ours_source, last_at, ours_ref, ours_code = self.ledger_status_at(a, D)
        if ours_status == "created":
            return []
        rows = self.statement_rows_at(a, D)
        p = self.providers[a.provider]
        out: list[tuple[str, dict[str, Any], str]] = []
        ev: dict[str, Any]
        if not rows:
            assert a.submitted_at is not None
            ev = {
                "ours_status": ours_status,
                "ours_reference": ours_ref,
                "submitted_at": a.submitted_at,
                "provider_lag_s": p.statement_lag_s,
                "provider": a.provider,
                "as_of": D,
            }
            if ours_status in ("submitted", "processing", "settled"):
                if ours_status == "settled" and not ours_ref:
                    return []
                ident = _digest({k: v for k, v in ev.items() if k != "as_of"})
                return [("missing_at_provider", ev, ident)]
            return []
        latest = rows[-1]
        dups = [r for r in rows if r.get("duplicate")]
        if dups:
            first = [r for r in rows if not r.get("duplicate") and r["status"] in TERMINAL][0]
            entries = [
                {
                    "status": first["status"],
                    "amount": first["amount"],
                    "reference": first["reference"],
                },
                {
                    "status": dups[0]["status"],
                    "amount": dups[0]["amount"],
                    "reference": dups[0]["reference"],
                },
            ]
            ev = {"entries": entries, "provider": a.provider, "as_of": D}
            out.append(("duplicate_at_provider", ev, _digest({"entries": entries})))
            latest = [r for r in rows if not r.get("duplicate")][-1]
        theirs = latest["status"]
        if theirs not in TERMINAL:
            return out
        if theirs == "cancelled" and latest.get("previously") in ("settled", "processing") and ours_status != "cancelled":
            ev = {
                "ours_status": ours_status,
                "theirs_status": theirs,
                "previously_reported": latest.get("previously"),
                "settlement_reference": latest.get("settlement_reference"),
                "theirs_entry_at": latest["entry_at"],
                "provider": a.provider,
                "as_of": D,
            }
            out.append(("late_cancel", ev, _digest({k: v for k, v in ev.items() if k != "as_of"})))
            return out
        if theirs in TERMINAL and ours_status not in TERMINAL:
            ev = {
                "ours_status": ours_status,
                "theirs_status": theirs,
                "theirs_reference": latest["reference"],
                "theirs_failure_code": latest.get("failure_code"),
                "theirs_entry_at": latest["entry_at"],
                "provider": a.provider,
                "as_of": D,
            }
            out.append(("ours_behind", ev, _digest({k: v for k, v in ev.items() if k != "as_of"})))
            return out
        if theirs in TERMINAL and ours_status in TERMINAL and theirs != ours_status:
            if theirs == "cancelled" and latest.get("previously") == "settled":
                ev = {
                    "ours_status": ours_status,
                    "theirs_status": theirs,
                    "settlement_reference": latest.get("settlement_reference"),
                    "theirs_entry_at": latest["entry_at"],
                    "provider": a.provider,
                    "as_of": D,
                }
                out.append(
                    ("late_cancel", ev, _digest({k: v for k, v in ev.items() if k != "as_of"}))
                )
                return out
            ev = {
                "ours_status": ours_status,
                "ours_status_source": ours_source,
                "ours_last_event_at": last_at,
                "ours_reference": ours_ref,
                "ours_failure_code": ours_code,
                "theirs_status": theirs,
                "theirs_reference": latest["reference"],
                "theirs_failure_code": latest.get("failure_code"),
                "theirs_entry_at": latest["entry_at"],
                "provider": a.provider,
                "as_of": D,
            }
            out.append(
                ("status_mismatch", ev, _digest({k: v for k, v in ev.items() if k != "as_of"}))
            )
            return out
        if theirs == "settled" and ours_status == "settled":
            if latest.get("return_reference") and (
                a.ledger_return_at is None or a.ledger_return_at > D
            ):
                ev = {
                    "return_reference": latest["return_reference"],
                    "theirs_return_amount": latest["return_amount"],
                    "ours_amount": a.amount_dst,
                    "return_at": latest["return_at"],
                    "provider": a.provider,
                    "as_of": D,
                }
                out.append(
                    ("late_return", ev, _digest({k: v for k, v in ev.items() if k != "as_of"}))
                )
                return out
            if latest["amount"] != a.amount_dst and (
                a.recon_booked_at is None or a.recon_booked_at > D
            ):
                ev = {
                    "ours_amount": a.amount_dst,
                    "theirs_amount": latest["amount"],
                    "fee_for_rail": a.fee_minor,
                    "fee_booked": a.fee_booked or (a.fee_booked_at is not None and a.fee_booked_at <= D),
                    "fx_tolerance": (a.amount_dst * FX_TOLERANCE_BPS) // 10_000,
                    "provider": a.provider,
                    "as_of": D,
                }
                out.append(
                    ("amount_mismatch", ev, _digest({k: v for k, v in ev.items() if k != "as_of"}))
                )
        return out

    def _resolve(self, rng: random.Random, br: dict[str, Any], D: int) -> None:
        res = br["correct_resolution"]
        at = D + rng.randint(HOUR, 36 * HOUR)
        # operators resolve on business days: a weekend finding waits for Monday morning
        local = local_date(at, OPERATOR_OFFSET)
        if local.weekday() >= 5:
            monday = local + dt.timedelta(days=7 - local.weekday())
            at = ts(monday.year, monday.month, monday.day, 9, 0, 0) + rng.randint(0, 8 * HOUR)
        br["resolution"] = res
        br["resolved_at"] = at
        br["state"] = "escalated" if res == "escalate" else "resolved"
        a = self.attempt_by_id.get(br["attempt_id"]) if br["attempt_id"] else None
        if a is None:
            return
        if res == "adopt_provider_status":
            rows = self.statement_rows_at(a, at)
            st_now = self.ledger_status_at(a, at)
            if st_now[0] in TERMINAL and st_now[1] != "timeout_assumed":
                rows = []  # the ledger learned it on its own before the operator acted
            if rows:
                latest = [r for r in rows if not r.get("duplicate")][-1]
                a.timeline.append(
                    (
                        at,
                        latest["status"],
                        "operator",
                        latest.get("reference"),
                        latest.get("failure_code"),
                    )
                )
                a.timeline.sort(key=lambda e: e[0])
                self._supersede_after(a, at)
                a.operator_events.append(
                    (at, "adopt_provider_status", {"break_id": br["break_id"], "status": latest["status"],
                                                   "reference": latest.get("reference"), "failure_code": latest.get("failure_code")})
                )
        elif res == "requery_provider":
            if (
                a.prov_status is not None
                and a.prov_terminal_at is not None
                and a.prov_terminal_at <= at + HOUR
                and self.ledger_status_at(a, at + HOUR)[0] not in TERMINAL
            ):
                st = a.prov_status
                a.timeline.append(
                    (
                        at + HOUR,
                        st,
                        "poll",
                        a.settlement_reference if st == "settled" else None,
                        a.raw_failure if st == "failed" else None,
                    )
                )
                a.timeline.sort(key=lambda e: e[0])
                a.operator_events.append((at, "requery_provider", {"break_id": br["break_id"]}))
                self._supersede_after(a, at + HOUR)
                self._event_counter += 1
                self.events.append(
                    Event(
                        event_id=f"evt_{self._event_counter:08d}", attempt_id=a.attempt_id, provider=a.provider,
                        provider_event_id=f"POLL{self._event_counter:08d}", occurred_at=at + HOUR,
                        raw_status=raw_status_for(a.provider, st), unified=st, source="poll", delivered_at=at + HOUR,
                        signature_valid=True, applied=True, apply_result="applied",
                        reference=a.settlement_reference if st == "settled" else None,
                        failure_code=a.raw_failure if st == "failed" else None, payload={"requery_for": br["break_id"]},
                    )
                )
        elif res in (
            "book_fee_difference",
            "book_fx_difference",
            "mark_false_duplicate",
            "request_reversal_of_duplicate",
        ):
            a.recon_booked_at = at
            if res == "book_fee_difference":
                a.fee_booked_at = at
            a.operator_events.append((at, res, {"break_id": br["break_id"]}))
        elif res == "book_return":
            if a.ledger_return_at is not None and a.ledger_return_at <= at:
                return  # the provider's own return reached the ledger before the operator booked it
            a.ledger_return_at = at
            a.operator_events.append((at, res, {"break_id": br["break_id"]}))
            for ev in self.events:
                if ev.attempt_id == a.attempt_id and ev.applied and ev.unified in ("return", "refund") and ev.delivered_at is not None and ev.delivered_at > at:
                    ev.applied = False
                    ev.apply_result = "return_already" if ev.unified == "return" else "refund_already"
            self._event_counter += 1
            self.events.append(Event(
                event_id=f"evt_{self._event_counter:08d}", attempt_id=a.attempt_id, provider=a.provider,
                provider_event_id="", occurred_at=at + 60, raw_status="refund", unified="refund", source="system",
                delivered_at=at + 60, signature_valid=True, applied=True, apply_result="applied",
                reference=a.return_reference, failure_code=None, payload={"refund_for_return": True}))
        elif res == "adopt_and_resubmit":
            st_now = self.ledger_status_at(a, at)
            if st_now[0] in TERMINAL and st_now[1] != "timeout_assumed":
                return  # the ledger learned the cancellation on its own before the operator acted
            a.timeline.append((at, "cancelled", "operator", None, "CANCELLED_BY_PROVIDER"))
            a.timeline.sort(key=lambda e: e[0])
            self._supersede_after(a, at)
            a.operator_events.append((at, res, {"break_id": br["break_id"], "status": "cancelled", "reference": None, "failure_code": "CANCELLED_BY_PROVIDER"}))
        elif res == "create_ledger_row":
            a.operator_events.append((at, res, {"break_id": br["break_id"]}))

    # ---- 8. RFIs ----
    def build_rfis(self) -> None:
        rng = self.S("rfis")
        n_total = max(40, int(round(len(self.payments) * RFI_PER_PAYMENT)))
        n_readable = int(round(n_total * (0.71 + rng.uniform(-0.004, 0.004))))
        n_template = int(round(n_total * (0.36 + rng.uniform(-0.004, 0.004))))
        n_readable_other = n_readable - n_template
        n_invoice = int(round(n_readable * (0.07 + rng.uniform(-0.004, 0.004))))
        n_unreadable = n_total - n_readable
        contra_users = [u for u in self.users if u["template_contradiction"]]
        attempts_by_user: dict[str, list[Attempt]] = {}
        for pm in self.payments:
            for a in pm.attempts:
                if a.submitted_at is not None:
                    attempts_by_user.setdefault(pm.end_user, []).append(a)
        rid = 0

        def new_rfi(
            a: Attempt,
            provider: str,
            readable: bool,
            rtype: str | None,
            template: bool,
            hard: list[str],
        ) -> dict[str, Any]:
            nonlocal rid
            rid += 1
            assert a.submitted_at is not None
            received = a.submitted_at + rng.randint(HOUR, 3 * DAY)
            if received > T0 - HOUR:
                received = a.submitted_at + rng.randint(600, 3600)
            if not readable:
                raw = rng.choice(
                    [
                        "",
                        "ref " + str(rng.randint(10**5, 10**6)),
                        "pls advise on the attached",
                        "code 4471",
                        "additional info required",
                        "see note",
                    ]
                )
            else:
                raw = rtype or ""
            user = self.user_by_id[self.payment_by_id[a.payment_id].end_user]
            as_of = received + rng.randint(HOUR, 20 * HOUR)
            if as_of > T0:
                as_of = T0
            case = {"readable": readable, "request_type": rtype}
            record = {"items": user["items"], "contradictions": user["contradictions"]}
            correct = correct_rfi_action(case, record, as_of)
            in_hist = received < MONITORING_START
            state_at_as_of = "in_review"
            final_state = {
                "answer_from_record": "answered",
                "request_from_customer": "customer_requested",
                "escalate_contradiction": "escalated",
                "request_clarification_from_provider": "clarification_requested",
            }[correct.action]
            r = {
                "rfi_id": f"rfi_{rid:06d}",
                "provider": provider,
                "attempt_id": a.attempt_id,
                "end_user": user["user_id"],
                "platform": user["platform"],
                "received_at": received,
                "readable": readable,
                "request_type": rtype,
                "raw_reason": raw,
                "template": template,
                "as_of": as_of,
                "state_at_as_of": state_at_as_of,
                "state_now": "closed"
                if in_hist
                else final_state
                if rng.random() < 0.3
                else "in_review",
                "follow_ups": rng.choice([0, 0, 0, 1, 2]),
                "correct": correct.as_dict(),
                "hard": list(hard),
                "in_monitoring": received >= MONITORING_START,
            }
            if not readable:
                r["hard"].append("H10")
            self.rfis.append(r)
            return r

        self.payment_by_id = {pm.payment_id: pm for pm in self.payments}
        # the template: one provider's one template about the template platform's contradiction users
        pair_users = [
            u
            for u in contra_users
            if any(a.provider == TEMPLATE_PROVIDER for a in attempts_by_user.get(u["user_id"], []))
            and any(a.provider != TEMPLATE_PROVIDER for a in attempts_by_user.get(u["user_id"], []))
        ]
        rng.shuffle(pair_users)
        n_h11 = min(len(pair_users), 69 if self.n_payments >= DEFAULT_PAYMENTS // 2 else 3)
        paired = 0
        for u in pair_users[:n_h11]:
            ha = [a for a in attempts_by_user[u["user_id"]] if a.provider == TEMPLATE_PROVIDER][0]
            oa = [a for a in attempts_by_user[u["user_id"]] if a.provider != TEMPLATE_PROVIDER][0]
            new_rfi(ha, TEMPLATE_PROVIDER, True, "employment_vs_source_of_funds", True, ["H11"])
            new_rfi(oa, oa.provider, True, "employment_vs_source_of_funds", False, ["H11"])
            paired += 1
        n_template -= paired
        n_readable_other -= paired
        template_attempts = [
            a
            for u in contra_users
            for a in attempts_by_user.get(u["user_id"], [])
            if a.provider == TEMPLATE_PROVIDER
        ]
        if not template_attempts:
            template_attempts = [
                a for u in contra_users for a in attempts_by_user.get(u["user_id"], [])
            ]
        rng.shuffle(template_attempts)
        for k in range(n_template):
            a = template_attempts[k % len(template_attempts)]
            new_rfi(a, TEMPLATE_PROVIDER, True, "employment_vs_source_of_funds", True, ["H11"])
        # other readable requests: invoice (6% of readable) and the record-holdable types; H12 expired-document ones
        all_attempts = [a for a in self.attempts if a.submitted_at is not None]
        rng.shuffle(all_attempts)
        ptr = 0
        types = [t for t in RFI_REQUEST_TYPES if t != "transaction_invoice"]
        n_h12 = self.counts["H12"]
        expired_users = [
            u
            for u in self.users
            if u["items"]["doc_identity"]["present"]
            and u["items"]["doc_identity"]["expires"] < "2026-06-01"
            and attempts_by_user.get(u["user_id"])
        ]
        h12 = 0
        for k in range(n_readable_other):
            a = all_attempts[ptr % len(all_attempts)]
            ptr += 1
            if k < n_invoice:
                new_rfi(a, a.provider, True, "transaction_invoice", False, [])
                continue
            if h12 < n_h12 and expired_users:
                u = expired_users[h12 % len(expired_users)]
                ua = attempts_by_user.get(u["user_id"], [])
                if ua:
                    new_rfi(rng.choice(ua), a.provider, True, "id_document_expired", False, ["H12"])
                    h12 += 1
                    continue
            new_rfi(a, a.provider, True, rng.choice(types), False, [])
        for _ in range(n_unreadable):
            a = all_attempts[ptr % len(all_attempts)]
            ptr += 1
            new_rfi(a, a.provider, False, None, False, [])
        self.rfis.sort(key=lambda r: (r["received_at"], r["rfi_id"]))

    # ---- 9. stuck and reroute pools ----
    def build_stuck(self) -> None:
        rng = self.S("stuck")
        grid = []
        t = MONITORING_START + 6 * HOUR
        while t <= T0:
            grid.append(t)
            t += 6 * HOUR
        inflight = [
            a
            for a in self.attempts
            if a.submitted_at is not None and a.submitted_at >= MONITORING_START - 7 * DAY
        ]
        sid = 0

        def case(a: Attempt, at: int, labels: list[str]) -> None:
            nonlocal sid
            status, _src, _last, _ref, _code = self.ledger_status_at(a, at)
            key = (a.provider, a.corridor, a.rail)
            prof = self.profiles[key]
            p95 = prof["p95"]
            stuck = evaluate_stuck(status, a.rail, a.submitted_at, at, p95, self.cal[a.corridor])
            degraded = self.degraded_at(key, at)
            sid += 1
            assert a.submitted_at is not None
            self.stuck_cases.append(
                {
                    "case_id": f"stk_{sid:06d}",
                    "attempt_id": a.attempt_id,
                    "as_of": at,
                    "provider": a.provider,
                    "rail": a.rail,
                    "corridor": a.corridor,
                    "status_at_as_of": status,
                    "submitted_at": a.submitted_at,
                    "elapsed": elapsed_on_clock(a.rail, a.submitted_at, at, self.cal[a.corridor]),
                    "unit": clock_unit(a.rail),
                    "p95": p95,
                    "profile_count": prof["count"],
                    "degraded_pair": degraded,
                    "correct": {"stuck": bool(stuck), "no_profile": p95 is None},
                    "hard": list(labels) + (["H16"] if labels or stuck else []),
                    "next_cutoff": next_cutoff(a.rail, at, self.cal[a.corridor]),
                }
            )

        for at in grid:
            for a in inflight:
                if a.submitted_at is None or a.submitted_at > at:
                    continue
                status = self.ledger_status_at(a, at)[0]
                if status in ("submitted", "processing"):
                    case(
                        a,
                        at,
                        ["degraded_pair"]
                        if self.degraded_at((a.provider, a.corridor, a.rail), at)
                        else [],
                    )
        for a in self.attempts:
            if a.plant and a.plant.startswith("not_stuck:"):
                at = a.stuck_as_of if a.stuck_as_of is not None else (a.submitted_at or a.created_at)
                case(a, at, [a.plant])
            elif a.plant == "settled_before_t":
                at = a.stuck_as_of if a.stuck_as_of is not None else (a.prov_terminal_at or 0) + 20
                st = self.ledger_status_at(a, at)[0]
                if st == "settled":
                    case(a, at, ["settled_before_t"])
            elif a.plant in ("stuck_clock", "stuck_calendar", "six_figure_wire"):
                key = (a.provider, a.corridor, a.rail)
                p95 = self.profiles[key]["p95"]
                if p95 is None or a.submitted_at is None:
                    continue
                if a.rail in CLOCK_RAILS:
                    at = a.submitted_at + (p95 + 30) * 60
                else:
                    at = (
                        next_cutoff(a.rail, a.submitted_at + 1, self.cal[a.corridor])
                        or a.submitted_at
                    )
                    for _ in range(p95):
                        at = next_cutoff(a.rail, at + 1, self.cal[a.corridor]) or at
                    at += 1800
                if at <= T0 and self.ledger_status_at(a, at)[0] in ("submitted", "processing"):
                    case(a, at, [a.plant])
        rng.shuffle(self.stuck_cases)
        self.stuck_cases.sort(key=lambda c: (c["as_of"], c["attempt_id"]))

    def build_reroutes(self) -> None:
        rid = 0
        self._held_by_episode: dict[str, list[Attempt]] = {}
        for a in self.attempts:
            if a.hold_until is None:
                continue
            for e in self.episodes:
                if (
                    a.provider == e["provider"]
                    and a.corridor == e["corridor"]
                    and a.rail == e["rail"]
                    and e["start"] <= a.created_at < e["end"]
                ):
                    self._held_by_episode.setdefault(e["episode_id"], []).append(a)
        for e in self.episodes:
            if not e["in_monitoring"]:
                continue
            key = (e["provider"], e["corridor"], e["rail"])
            t = e["start"]
            while t <= e["end"]:
                if self.degraded_at(key, t):
                    rid += 1
                    self.reroute_cases.append(self._reroute_case(rid, e, t))
                t += 2 * 60
        self.reroute_cases.sort(key=lambda c: (c["as_of"], c["case_id"]))

    def _reroute_case(self, rid: int, e: dict[str, Any], t: int) -> dict[str, Any]:
        corridor = e["corridor"]
        pairs: list[PairState] = []
        health = []
        for name in PROVIDER_NAMES:
            for c, rail in COVERAGE[name]:
                if c != corridor:
                    continue
                key = (name, c, rail)
                n, f = self.trailing(key, t)
                deg = self.degraded_at(key, t)
                p95 = self.profiles[key]["p95"]
                pairs.append(PairState(name, rail, deg, p95))
                health.append(
                    {
                        "provider": name,
                        "rail": rail,
                        "attempts_24h": n,
                        "failures_24h": f,
                        "share": round(f / n, 4) if n else 0.0,
                        "baseline": self.baseline[key],
                        "degraded": deg,
                        "p95": p95,
                    }
                )
        held = [
            a
            for a in self._held_by_episode.get(e["episode_id"], [])
            if a.created_at <= t and (a.submitted_at is None or a.submitted_at > t)
        ]
        amount = max((a.amount_dst for a in held), default=0)
        same = [p for p in pairs if p.rail == e["rail"]]
        dest = route(corridor, amount, same, exclude=(e["provider"], e["rail"]))
        if dest is None:
            others = [p for p in pairs if p.rail != e["rail"]]
            dest = route(corridor, amount, others)
        hard = []
        if dest is None:
            hard.append("H17_no_healthy")
        elif dest[1] in CALENDAR_RAILS:
            hard.append("H17_calendar_alternative")
        return {
            "case_id": f"rr_{rid:06d}",
            "episode_id": e["episode_id"],
            "corridor": corridor,
            "from_provider": e["provider"],
            "from_rail": e["rail"],
            "as_of": t,
            "pairs": health,
            "created_payouts": sorted(a.attempt_id for a in held),
            "correct": {
                "to_provider": dest[0] if dest else None,
                "to_rail": dest[1] if dest else None,
                "hold": dest is None,
                "payouts": sorted(a.attempt_id for a in held),
                "settlement_moves_to_next_cycle": bool(dest and dest[1] in CALENDAR_RAILS),
            },
            "hard": hard,
        }

    # ---- 10. reporting: the summary, the questions, the planted wrong values ----
    def status_as_of(self, pm: Payment, at: int) -> tuple[Attempt | None, str, bool]:
        latest = None
        for a in pm.attempts:
            if a.created_at <= at:
                latest = a
        if latest is None:
            return None, "none", False
        st = self.ledger_status_at(latest, at)[0]
        returned = latest.ledger_return_at is not None and latest.ledger_return_at <= at
        return latest, st, returned

    def build_reporting(self) -> None:
        rng = self.S("reporting")
        countries = ["MX", "BR", "PH", "DE", "US"]
        # the planted summary: attempts-based, as of Friday 2026-10-02 18:00, returns folded into failed
        R = SUMMARY_REFRESHED_AT
        agg: dict[tuple[str, int, str, str], dict[str, Any]] = {}
        for pm in self.payments:
            for a in pm.attempts:
                if a.created_at > R:
                    continue
                st = self.ledger_status_at(a, R)[0]
                returned = a.ledger_return_at is not None and a.ledger_return_at <= R
                key = (pm.corridor, operator_week(a.created_at), a.provider, a.rail)
                row = agg.setdefault(
                    key, {"n": 0, "volume": 0, "n_failed": 0, "failed": 0, "n_settled": 0}
                )
                row["n"] += 1
                row["volume"] += a.amount_usd_cents
                if st in ("failed", "cancelled") or returned:
                    row["n_failed"] += 1
                    row["failed"] += a.amount_usd_cents
                elif st == "settled":
                    row["n_settled"] += 1
        for (c, w, prov, rail), row in sorted(agg.items()):
            self.summary_rows.append(
                {
                    "corridor": c,
                    "week": w,
                    "provider": prov,
                    "rail": rail,
                    "n_payments": row["n"],
                    "volume_usd_cents": row["volume"],
                    "n_failed": row["n_failed"],
                    "failed_usd_cents": row["failed"],
                    "n_settled": row["n_settled"],
                    "refreshed_at": R,
                }
            )
        # question instances
        as_ofs = []
        hourly = []
        dense = self.n_payments >= DEFAULT_PAYMENTS // 2
        d = dt.date(2026, 9, 8)
        while d <= dt.date(2026, 10, 4):
            as_ofs.append(ts(d.year, d.month, d.day, 9, 0, 0))
            if dense:
                as_ofs.append(ts(d.year, d.month, d.day, 17, 0, 0))
                for h in range(24):
                    hourly.append(ts(d.year, d.month, d.day, h, 0, 0))
            d = d + dt.timedelta(days=1)
        as_ofs.append(T0)
        hourly.append(T0)
        if not dense:
            hourly = list(as_ofs)
        qid = 0
        hard_payments: dict[str, list[tuple[Payment, str]]] = {}
        for pm in self.payments:
            for label in pm.hard:
                if label in ("H18", "H19", "H20", "H21", "H22"):
                    hard_payments.setdefault(pm.corridor, []).append((pm, label))
        for country in countries:
            for at in as_ofs:
                cur_week = operator_week(at)
                weeks = [w for w in range(27, 41) if w < cur_week or (at == T0 and w <= 40)]
                windows: list[dict[str, Any]] = [{"kind": "week", "week": w} for w in weeks]
                windows.append(
                    {"kind": "range", "start": at - 7 * DAY, "end": at, "label": "last 7 days"}
                )
                for win in windows:
                    qid += 1
                    self.questions.append(
                        self._question(
                            qid, "corridor_volume_failed", country, at, win, hard_payments, rng
                        )
                    )
                    if win["kind"] == "week" and (dense or win["week"] >= cur_week - 2 or at == T0):
                        for by in ("provider", "rail"):
                            qid += 1
                            self.questions.append(
                                self._question(
                                    qid,
                                    "failure_share_by",
                                    country,
                                    at,
                                    {**win, "by": by},
                                    hard_payments,
                                    rng,
                                )
                            )
            for at in hourly:
                qid += 1
                self.questions.append(
                    self._question(
                        qid,
                        "status_counts",
                        country,
                        at,
                        {"kind": "range", "start": at - _SCL, "end": at},
                        hard_payments,
                        rng,
                    )
                )

    def _in_window(self, pm: Payment, win: dict[str, Any], family: str) -> bool:
        """Whether a payment belongs to a question's window, as the glossary and its SQL say: a week
        by its operator-time ISO week, a "last 7 days" range up to but not including the as-of
        time, and the status counts' 30 days up to and including it (a payment created at the
        as-of instant is in flight then)."""
        if win["kind"] == "week":
            return bool(operator_week(pm.created_at) == win["week"])
        if family == "status_counts":
            return bool(win["start"] <= pm.created_at <= win["end"])
        return bool(win["start"] <= pm.created_at < win["end"])

    def _question(
        self,
        qid: int,
        family: str,
        country: str,
        at: int,
        win: dict[str, Any],
        hard_payments: dict[str, list[tuple[Payment, str]]],
        rng: random.Random,
    ) -> dict[str, Any]:
        pms = [
            pm
            for pm in self.payments
            if pm.corridor == country and pm.created_at <= at and self._in_window(pm, win, family)
        ]
        hard: list[str] = []
        if (
            win["kind"] == "week"
            and win["week"] == 40
            or (win["kind"] == "range" and win["end"] == at and at >= week_bounds(40)[0] + DAY)
        ):
            for pm, h in hard_payments.get(country, []):
                if pm.created_at <= at and self._in_window(pm, win, family) and h not in hard:
                    hard.append(h)
        correct: dict[str, Any]
        wrong: dict[str, Any] = {}
        cands: dict[str, Any]
        if family in ("corridor_volume_failed", "status_counts"):
            vol = n = failed_usd = n_failed = returned_usd = in_flight_usd = n_in_flight = (
                n_returned
            ) = 0
            att_vol = att_n = att_failed = att_n_failed = 0
            failed_with_returns = 0
            failed_without_cancelled = 0
            local_week_vol = 0
            for pm in pms:
                latest, st, returned = self.status_as_of(pm, at)
                vol += pm.amount_usd_cents
                n += 1
                if st in ("failed", "cancelled"):
                    failed_usd += pm.amount_usd_cents
                    n_failed += 1
                    failed_with_returns += pm.amount_usd_cents
                    if st == "failed":
                        failed_without_cancelled += pm.amount_usd_cents
                elif st == "settled" and returned:
                    returned_usd += pm.amount_usd_cents
                    n_returned += 1
                    failed_with_returns += pm.amount_usd_cents
                elif st not in TERMINAL:
                    in_flight_usd += pm.amount_usd_cents
                    n_in_flight += 1
                for a in pm.attempts:
                    if a.created_at <= at:
                        att_vol += pm.amount_usd_cents
                        att_n += 1
                        ast = self.ledger_status_at(a, at)[0]
                        if ast in ("failed", "cancelled"):
                            att_failed += pm.amount_usd_cents
                            att_n_failed += 1
            if win["kind"] == "week":
                # the trap of a local-time week (Berlin or Manila) by created_at
                off = ZONE_OFFSET[country]
                for pm in self.payments:
                    if (
                        pm.corridor == country
                        and pm.created_at <= at
                        and local_date(pm.created_at, off).isocalendar()[1] == win["week"]
                    ):
                        local_week_vol += pm.amount_usd_cents
            if family == "corridor_volume_failed":
                correct = {
                    "volume_usd": vol / 100,
                    "n_payments": n,
                    "failed_usd": failed_usd / 100,
                    "n_failed": n_failed,
                    "returned_usd": returned_usd / 100,
                    "in_flight_usd": in_flight_usd / 100,
                }
                cands = {
                    "attempts_volume_usd": att_vol / 100,
                    "attempts_n_payments": att_n,
                    "attempts_failed_usd": att_failed / 100,
                    "attempts_n_failed": att_n_failed,
                    "failed_with_returns_usd": failed_with_returns / 100,
                    "failed_without_cancelled_usd": failed_without_cancelled / 100,
                }
                if win["kind"] == "week":
                    cands["local_time_week_volume_usd"] = local_week_vol / 100
                    srow = self._summary_for(country, win["week"])
                    if srow:
                        cands["summary_volume_usd"] = srow["volume_usd_cents"] / 100
                        cands["summary_n_payments"] = srow["n_payments"]
                        cands["summary_failed_usd"] = srow["failed_usd_cents"] / 100
                        cands["summary_n_failed"] = srow["n_failed"]
                wrong = {k: v for k, v in cands.items() if v != correct[_corr_key(k)]}
            else:
                correct = {
                    "n_in_flight": n_in_flight,
                    "in_flight_usd": in_flight_usd / 100,
                    "n_returned": n_returned,
                    "returned_usd": returned_usd / 100,
                    "n_failed": n_failed,
                    "failed_usd": failed_usd / 100,
                }
                cands = {
                    "attempts_n_failed": att_n_failed,
                    "attempts_failed_usd": att_failed / 100,
                    "failed_with_returns_usd": failed_with_returns / 100,
                    "returns_as_failed_n": n_failed + n_returned,
                }
                wrong = {k: v for k, v in cands.items() if v != correct[_corr_key(k)]}
        else:
            by = win["by"]
            groups: dict[str, list[int]] = {}
            att_groups: dict[str, list[int]] = {}
            for pm in pms:
                latest, st, _ = self.status_as_of(pm, at)
                if latest is None:
                    continue
                g = latest.provider if by == "provider" else latest.rail
                if st in TERMINAL:
                    groups.setdefault(g, []).append(1 if st in ("failed", "cancelled") else 0)
                for a in pm.attempts:
                    if a.created_at <= at:
                        ast = self.ledger_status_at(a, at)[0]
                        if ast in TERMINAL:
                            att_groups.setdefault(
                                a.provider if by == "provider" else a.rail, []
                            ).append(1 if ast in ("failed", "cancelled") else 0)
            best = None
            for g, vals in groups.items():
                share = round(sum(vals) / len(vals), 4)
                key = (-share, g)
                if best is None or key < best[0]:
                    best = (key, g, share, sum(vals), len(vals))
            correct = (
                {"by": best[1], "share": best[2], "n_failed": best[3], "n_terminal": best[4]}
                if best
                else {"by": None, "share": None, "n_failed": 0, "n_terminal": 0}
            )
            abest = None
            for g, vals in att_groups.items():
                share = round(sum(vals) / len(vals), 4)
                key = (-share, g)
                if abest is None or key < abest[0]:
                    abest = (key, g, share)
            cands = {}
            if abest:
                cands["attempts_share"] = abest[2]
                cands["attempts_by"] = abest[1]
            srows = [
                r
                for r in self.summary_rows
                if r["corridor"] == country and r["week"] == win["week"]
            ]
            if srows:
                sg: dict[str, list[int]] = {}
                for r in srows:
                    g = r["provider"] if by == "provider" else r["rail"]
                    sg.setdefault(g, [0, 0])
                    sg[g][0] += r["n_failed"]
                    sg[g][1] += r["n_failed"] + r["n_settled"]
                sbest = None
                for g, (f, tot) in sg.items():
                    if tot == 0:
                        continue
                    share = round(f / tot, 4)
                    key = (-share, g)
                    if sbest is None or key < sbest[0]:
                        sbest = (key, g, share)
                if sbest:
                    cands["summary_share"] = sbest[2]
                    cands["summary_by"] = sbest[1]
            wrong = {k: v for k, v in cands.items() if v != correct[_corr_key(k)]}
        if not wrong:
            return {}
        return {
            "instance_id": f"q_{qid:06d}",
            "family": family,
            "country": country,
            "as_of": at,
            "window": win,
            "correct": correct,
            "planted_wrong_values": wrong,
            "hard": hard,
            "text": self._question_text(family, country, at, win),
        }

    def _summary_for(self, country: str, week: int) -> dict[str, Any] | None:
        rows = [r for r in self.summary_rows if r["corridor"] == country and r["week"] == week]
        if not rows:
            return None
        return {
            "volume_usd_cents": sum(r["volume_usd_cents"] for r in rows),
            "n_payments": sum(r["n_payments"] for r in rows),
            "failed_usd_cents": sum(r["failed_usd_cents"] for r in rows),
            "n_failed": sum(r["n_failed"] for r in rows),
        }

    def _question_text(self, family: str, country: str, at: int, win: dict[str, Any]) -> str:
        names = {
            "MX": "Mexico",
            "BR": "Brazil",
            "PH": "the Philippines",
            "DE": "Germany",
            "US": "the United States",
        }
        if win["kind"] == "week":
            w = "last week" if win["week"] == operator_week(at) - 1 else f"ISO week {win['week']}"
        else:
            w = win.get("label", "the last 30 days")
        if family == "corridor_volume_failed":
            return f"How much went to {names[country]} {w}, and how much of it failed?"
        if family == "failure_share_by":
            return f"Which {win['by']} had the highest failure share on payouts to {names[country]} {w}?"
        return f"How many payouts to {names[country]} are in flight, returned and failed as of now?"

    # ---- 11. hard-case counts and labels ----
    def count_hard(self) -> None:
        c: dict[str, int] = {}

        def add(label: str, k: int = 1) -> None:
            c[label] = c.get(label, 0) + k

        for br in self.breaks:
            for h in br["hard"]:
                add(h)
            if br["plant"] == "H4_true":
                add("H4_true_duplicate")
            if br["plant"] == "H4_false":
                add("H4_false_duplicate")
        for a in self.attempts:
            if "H6" in a.hard:
                add("H6")
            if "H9" in a.hard:
                add("H9")
        for rf in self.rfis:
            for h in rf["hard"]:
                add(h)
        h11_users: dict[str, set[str]] = {}
        for rf in self.rfis:
            if "H11" in rf["hard"]:
                h11_users.setdefault(rf["end_user"], set()).add(rf["provider"])
        c["H11_users_two_providers"] = sum(1 for v in h11_users.values() if len(v) >= 2)
        for s in self.stuck_cases:
            if "H16" in s["hard"]:
                add("H16")
            if s["correct"]["stuck"]:
                add("H16_stuck")
            else:
                add("H16_not_stuck")
        for rr in self.reroute_cases:
            for h in rr["hard"]:
                add(h)
        for pm in self.payments:
            for h in pm.hard:
                add(h)
        # H7: the largest number of ours_behind breaks found by one pass
        per_pass: dict[str, int] = {}
        for br in self.breaks:
            if br["kind"] == "ours_behind":
                per_pass[br["first_pass"]] = per_pass.get(br["first_pass"], 0) + 1
        c["H7_max_ours_behind_in_one_pass"] = max(per_pass.values(), default=0)
        self.hard_counts = c

    # ---- 12. write ----
    def write(self, out: Path) -> dict[str, Any]:
        out.mkdir(parents=True, exist_ok=True)
        db = out / "world.sqlite"
        if db.exists():
            db.unlink()
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        self._write_rows(conn)
        conn.commit()
        conn.close()
        self.answer_questions_by_rule(db)
        oracle = self.oracle()
        (out / "oracle.json").write_text(
            json.dumps(oracle, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        ctx = out / "context"
        ctx.mkdir(exist_ok=True)
        for name, text in render_context(self).items():
            (ctx / name).write_text(text, encoding="utf-8")
        return oracle

    def answer_questions_by_rule(self, db: Path) -> None:
        """Take every question's correct answer from the one rule function, `glossary_answer`, by
        SQL over the tables just written, and refuse the world when the generator's own count from
        its event log differs from it anywhere (a disagreement is a generator defect, never an
        answer to write). Reads run on read-only connections, a few at a time; each answer is kept
        under its question, so the order they finish in reaches nothing."""
        questions = list(self.questions)
        chunks = [questions[i::RULE_ANSWER_READERS] for i in range(RULE_ANSWER_READERS)]

        def answer(chunk: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                return [
                    (q["instance_id"], glossary_answer(conn, q["family"], rule_params(q)))
                    for q in chunk
                ]
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=RULE_ANSWER_READERS) as pool:
            answers = {i: a for part in pool.map(answer, chunks) for i, a in part}
        differ = sorted(q["instance_id"] for q in questions if answers[q["instance_id"]] != q["correct"])
        if differ:
            first = differ[0]
            q = next(q for q in questions if q["instance_id"] == first)
            raise OracleDisagreement(
                f"{len(differ)} of {len(questions)} questions: the generator's count from its event "
                f"log differs from the rule function's answer over the written tables; first "
                f"{first}: generator {q['correct']}, rule {answers[first]}"
            )
        for q in questions:
            q["correct"] = answers[q["instance_id"]]

    def _write_rows(self, conn: sqlite3.Connection) -> None:
        S = self.src
        conn.executemany(
            "INSERT INTO platforms VALUES (?,?,?)",
            [
                (p["platform_id"], p["name"], json.dumps(p["corridors"], sort_keys=True))
                for p in self.platforms
            ],
        )
        conn.executemany(
            "INSERT INTO identity_records VALUES (?,?,?,?,?,?)",
            [
                (
                    u["user_id"],
                    u["platform"],
                    u["name"],
                    json.dumps(u["items"], sort_keys=True),
                    json.dumps(u["contradictions"]),
                    int(u["template_contradiction"]),
                )
                for u in self.users
            ],
        )
        for name in PROVIDER_NAMES:
            p = self.providers[name]
            conn.execute(
                "INSERT INTO providers VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    json.dumps([list(x) for x in COVERAGE[name]]),
                    p.statement_lag_s,
                    p.webhook_duplicate,
                    p.webhook_out_of_order,
                    p.webhook_late,
                    p.webhook_drop,
                    p.webhook_invalid_sig,
                    p.retry_share,
                    json.dumps(
                        {
                            f"{c}|{r}": {"flat_minor": p.fee_flat[(c, r)], "bps": p.fee_bps[(c, r)]}
                            for c, r in COVERAGE[name]
                        },
                        sort_keys=True,
                    ),
                ),
            )
            from .providers import REASON_VOCABULARY, STATUS_VOCABULARY, VERDANT_KEYWORDS

            for raw, uni in STATUS_VOCABULARY[name].items():
                conn.execute(
                    "INSERT INTO provider_vocabulary VALUES (?,?,?,?)", (name, "status", raw, uni)
                )
            for raw, uni in REASON_VOCABULARY[name].items():
                conn.execute(
                    "INSERT INTO provider_vocabulary VALUES (?,?,?,?)", (name, "reason", raw, uni)
                )
            if name == "Verdant":
                for kw, uni in VERDANT_KEYWORDS:
                    conn.execute(
                        "INSERT INTO provider_vocabulary VALUES (?,?,?,?)",
                        (name, "reason_keyword", kw, uni),
                    )
            for c in sorted(p.markup):
                fl = self.floors[c]
                conn.execute(
                    "INSERT INTO fx_markup_model VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        name,
                        c,
                        round(p.markup[c], 6),
                        f"{MARKUP_BAND_PCT[0]}% to {MARKUP_BAND_PCT[1]}%",
                        FX_PROXY_SENTENCE,
                        fl.floor_pct,
                        fl.market_spread_pct,
                        fl.source,
                        f"{fl.floor_pct}% to {MARKUP_BAND_PCT[1]}%",
                    ),
                )
        for rail, doc in S.rails["rails"].items():
            conn.execute(
                "INSERT INTO rails VALUES (?,?,?,?,?,?)",
                (
                    rail,
                    doc["destination"],
                    doc["clock"],
                    json.dumps(doc, sort_keys=True),
                    RAIL_LIMIT_MINOR.get(rail),
                    json.dumps([s["url"] for s in doc["sources"]]),
                ),
            )
        for dest, cal in self.cal.items():
            doc = S.calendar_docs[dest]
            d = local_date(WINDOW_START, cal.offset)
            end = local_date(WINDOW_END, cal.offset) + dt.timedelta(days=1)
            while d <= end:
                conn.execute(
                    "INSERT INTO calendars VALUES (?,?,?,?)",
                    (dest, d.isoformat(), int(cal.is_banking_day(d)), doc["sources"][0]["url"]),
                )
                d = d + dt.timedelta(days=1)
        for corridor, fix_rows in S.fixings.items():
            meta = S.fixing_meta[corridor]
            for fr in fix_rows:
                conn.execute(
                    "INSERT INTO fx_mid VALUES (?,?,?,?,?,?,?,?)",
                    (
                        corridor,
                        CORRIDOR_CURRENCY[corridor],
                        fr.date.isoformat(),
                        round(fr.rate, 8),
                        fr.published,
                        int(fr.banking_day),
                        fr.source_date.isoformat(),
                        meta.get("cite_url", meta["url"]),
                    ),
                )
                if corridor == "DE":
                    pass
        conn.execute(
            "INSERT INTO fx_mid VALUES (?,?,?,?,?,?,?,?)",
            ("US", "USD", "all", 1.0, "1", 1, "all", S.fees["usd_at_par"]["url"]),
        )
        for pm in self.payments:
            latest = pm.attempts[-1] if pm.attempts else None
            st = self.ledger_status_at(latest, T0)[0] if latest else "created"
            returned = (
                latest is not None
                and latest.ledger_return_at is not None
                and latest.ledger_return_at <= T0
            )
            open_breaks = [
                b
                for b in self.breaks
                if latest
                and b["attempt_id"] == latest.attempt_id
                and b["state"] in ("open", "escalated")
            ]
            recon = (
                "exception"
                if open_breaks
                else "matched"
                if latest and self.statement_rows_at(latest, T0)
                else "pending"
            )
            conn.execute(
                "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pm.payment_id,
                    pm.platform,
                    pm.end_user,
                    pm.corridor,
                    pm.currency,
                    pm.amount_usd_cents,
                    iso_utc(pm.created_at),
                    latest.attempt_id if latest else None,
                    st,
                    "returned" if returned else None,
                    latest.provider if latest else None,
                    latest.rail if latest else None,
                    len(pm.attempts),
                    recon,
                ),
            )
        for a in sorted(self.attempts, key=lambda x: x.attempt_id):
            st, src, last, ref, code = self.ledger_status_at(a, T0)
            term_at, term_status = self.terminal_of(a)
            assumed_at = None
            assume_t = self.assumed_failed_from(a)
            if assume_t is not None and assume_t <= T0 and self.ledger_status_at(a, assume_t - 1)[0] not in TERMINAL:
                assumed_at = assume_t
            conn.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a.attempt_id,
                    a.payment_id,
                    a.n,
                    a.corridor,
                    a.provider,
                    a.rail,
                    iso_utc(a.created_at),
                    iso_utc(a.submitted_at),
                    iso_utc(term_at),
                    term_status,
                    iso_utc(assumed_at),
                    st,
                    src,
                    a.provider_reference,
                    ref if st == "settled" else None,
                    code if st == "failed" else None,
                    a.failure_reason if st == "failed" or st == "cancelled" else None,
                    int(a.reason_unmapped),
                    iso_utc(a.ledger_return_at),
                    a.return_reason if a.ledger_return_at else None,
                    a.amount_usd_cents,
                    a.amount_dst,
                    a.fee_minor,
                    int(a.fee_booked or a.fee_booked_at is not None),
                    round(a.fx_quote, 8),
                    a.client_reference,
                ),
            )
            conn.execute(
                "INSERT INTO fx_quotes VALUES (?,?,?,?,?,?)",
                (
                    a.attempt_id,
                    a.corridor,
                    round(a.fx_mid, 8),
                    round(a.fx_markup, 6),
                    round(a.fx_quote, 8),
                    FX_PROXY_SENTENCE,
                ),
            )
            stmt_rows = a.entries[0]["rows"] if a.entries and a.entries[0].get("kind") == "stmt" else []
            for sr in stmt_rows:
                conn.execute(
                    "INSERT INTO statements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sr["entry_id"],
                        a.provider,
                        a.attempt_id,
                        a.client_reference,
                        iso_utc(sr["entry_at"]),
                        raw_status_for(a.provider, sr["status"]),
                        sr["status"],
                        sr["amount"],
                        sr["fee"],
                        sr.get("reference"),
                        sr.get("failure_code"),
                        sr.get("return_reference"),
                        sr.get("return_amount"),
                    ),
                )
            for t, kind, extra in a.operator_events:
                self._event_counter += 1
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"evt_{self._event_counter:08d}",
                        a.attempt_id,
                        a.provider,
                        "",
                        iso_utc(t),
                        kind,
                        kind,
                        "operator",
                        iso_utc(t),
                        1,
                        1,
                        "applied",
                        None,
                        json.dumps(extra),
                    ),
                )
        for o in self.orphan_entries:
            conn.execute(
                "INSERT INTO statements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    o["entry_id"],
                    o["provider"],
                    None,
                    o["client_reference"],
                    iso_utc(o["entry_at"]),
                    raw_status_for(o["provider"], o["status"] or "settled"),
                    o["status"],
                    o["amount"],
                    o["fee"],
                    o["reference"],
                    None,
                    None,
                    None,
                ),
            )
        for ev in sorted(self.events, key=lambda e: e.event_id):
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.event_id,
                    ev.attempt_id,
                    ev.provider,
                    ev.provider_event_id,
                    iso_utc(ev.occurred_at),
                    ev.raw_status,
                    ev.unified,
                    ev.source,
                    iso_utc(ev.delivered_at),
                    int(ev.signature_valid),
                    int(ev.applied),
                    ev.apply_result,
                    ev.reference,
                    json.dumps({**ev.payload, **({"failure_code": ev.failure_code} if ev.failure_code else {})}, sort_keys=True),
                ),
            )
        for (prov, c, rail), prof in sorted(self.profiles.items()):
            conn.execute(
                "INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?)",
                (
                    prov,
                    c,
                    rail,
                    prof["count"],
                    prof["p50"],
                    prof["p95"],
                    prof["unit"],
                    self.baseline[(prov, c, rail)],
                ),
            )
        for ps in self.passes:
            conn.execute(
                "INSERT INTO passes VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ps["pass_id"],
                    ps["cadence"],
                    iso_utc(ps["run_at"]),
                    ps["lookback_days"],
                    ps["compared"],
                    ps["matched"],
                    ps["match_rate"],
                    ps["raised"],
                    ps["reobserved"],
                ),
            )
        for b in self.breaks:
            conn.execute(
                "INSERT INTO breaks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    b["break_id"],
                    b["attempt_id"],
                    b["kind"],
                    json.dumps(b["evidence"], sort_keys=True),
                    b["evidence_digest"],
                    b["version"],
                    int(b["superseded"]),
                    iso_utc(b["found_at"]),
                    b["first_pass"],
                    json.dumps(b["seen_in_passes"]),
                    b["state"],
                    iso_utc(b["resolved_at"]),
                    b["resolution"],
                    b.get("statement_entry_id"),
                ),
            )
            conn.execute(
                "INSERT INTO break_versions VALUES (?,?,?,?,?)",
                (b["break_id"], b["attempt_id"], b["kind"], b["version"], b["evidence_digest"]),
            )
        for rf in self.rfis:
            conn.execute(
                "INSERT INTO rfis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rf["rfi_id"],
                    rf["provider"],
                    rf["attempt_id"],
                    rf["end_user"],
                    rf["platform"],
                    iso_utc(rf["received_at"]),
                    int(rf["readable"]),
                    rf["request_type"],
                    rf["raw_reason"],
                    int(rf["template"]),
                    rf["state_now"],
                    rf["follow_ups"],
                    iso_utc(rf["as_of"]),
                ),
            )
        for inc in self.incidents:
            conn.execute(
                "INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                (
                    inc["incident_id"],
                    inc["provider"],
                    inc["service"],
                    inc["corridor"],
                    inc["rail"],
                    iso_utc(inc["first_observed"]),
                    iso_utc(inc["resolved_at"]),
                    inc["episode_id"],
                ),
            )
        for e in self.episodes:
            conn.execute(
                "INSERT INTO degradation_windows VALUES (?,?,?,?,?,?,?,?)",
                (
                    e["episode_id"],
                    e["provider"],
                    e["corridor"],
                    e["rail"],
                    iso_utc(e["start"]),
                    iso_utc(e["end"]),
                    e["failure_share"],
                    e["label"],
                ),
            )
        for w in self.webhook_incidents:
            conn.execute(
                "INSERT INTO webhook_incidents VALUES (?,?,?,?)",
                (w["incident_id"], w["provider"], iso_utc(w["start"]), iso_utc(w["end"])),
            )
        for sm in self.summary_rows:
            conn.execute(
                "INSERT INTO corridor_weekly_summary VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sm["corridor"],
                    sm["week"],
                    sm["provider"],
                    sm["rail"],
                    sm["n_payments"],
                    sm["volume_usd_cents"],
                    sm["n_failed"],
                    sm["failed_usd_cents"],
                    sm["n_settled"],
                    iso_utc(sm["refreshed_at"]),
                ),
            )
        meta = {
            "synthetic": "true",
            "seed": str(self.seed),
            "generator_version": GENERATOR_VERSION,
            "payments_requested": str(self.n_payments),
            "window_start": iso_utc(WINDOW_START) or "",
            "window_end": iso_utc(WINDOW_END) or "",
            "now": iso_utc(T0) or "",
            "history_end": iso_utc(HISTORY_END) or "",
            "monitoring_start": iso_utc(MONITORING_START) or "",
            "operator_zone": "America/New_York (UTC-4 for the whole window)",
            "sentence": SYNTHETIC_SENTENCE,
            "fx_sentence": FX_PROXY_SENTENCE,
            "profile_min_count": str(self.profile_min_count),
            "declared_corridor_mix": json.dumps(CORRIDOR_MIX, sort_keys=True),
            "declared_amount_median_usd": json.dumps(AMOUNT_MEDIAN_USD, sort_keys=True),
            "markup_band_pct": f"{MARKUP_BAND_PCT[0]} to {MARKUP_BAND_PCT[1]}",
            "markup_floors_pct": json.dumps(
                {c: f.floor_pct for c, f in sorted(self.floors.items())}, sort_keys=True
            ),
            "band_sentence": BAND_SENTENCE,
            "sources_manifest": json.dumps(self.src.manifest, sort_keys=True),
        }
        for k, v in sorted(meta.items()):
            conn.execute("INSERT INTO world_meta VALUES (?,?)", (k, v))

    def oracle(self) -> dict[str, Any]:
        cases: dict[str, Any] = {}
        for r in self.rfis:
            cases[r["rfi_id"]] = {
                "action_type": "rfi",
                "as_of": r["as_of"],
                "provider": r["provider"],
                "end_user": r["end_user"],
                "readable": r["readable"],
                "request_type": r["request_type"],
                "correct": r["correct"],
                "hard": r["hard"],
                "template": r["template"],
                "in_monitoring": r["in_monitoring"],
            }
        for b in self.breaks:
            cases[b["break_id"]] = {
                "action_type": "break",
                "as_of": b["as_of"],
                "kind": b["kind"],
                "attempt_id": b["attempt_id"],
                "evidence": b["evidence"],
                "correct": {"resolution": b["correct_resolution"]},
                "hard": b["hard"],
                "plant": b["plant"],
                "in_monitoring": b["found_at"] >= MONITORING_START,
                "version": b["version"],
            }
        for s in self.stuck_cases:
            cases[s["case_id"]] = {
                "action_type": "stuck",
                **{k: v for k, v in s.items() if k != "case_id"},
                "in_monitoring": s["as_of"] >= MONITORING_START,
            }
        for rr in self.reroute_cases:
            cases[rr["case_id"]] = {
                "action_type": "reroute",
                **{k: v for k, v in rr.items() if k != "case_id"},
                "in_monitoring": True,
            }
        questions = {
            q["instance_id"]: {k: v for k, v in q.items() if k != "instance_id"}
            for q in self.questions
        }
        return {
            "sentence": SYNTHETIC_SENTENCE,
            "fx_sentence": FX_PROXY_SENTENCE,
            "seed": self.seed,
            "generator_version": GENERATOR_VERSION,
            "window": {"start": WINDOW_START, "end": WINDOW_END, "now": T0},
            "cases": cases,
            "questions": questions,
            "hard_counts": self.hard_counts,
            "episodes": self.episodes,
            "webhook_incidents": self.webhook_incidents,
            "planted_summary_refreshed_at": SUMMARY_REFRESHED_AT,
            "pools": self.pools(),
        }

    def pools(self) -> dict[str, Any]:
        return {
            "rfi": [r["rfi_id"] for r in self.rfis],
            "break": [b["break_id"] for b in self.breaks],
            "stuck": [s["case_id"] for s in self.stuck_cases if s["as_of"] >= MONITORING_START],
            "reroute": [r["case_id"] for r in self.reroute_cases],
            "questions": {
                fam: [q["instance_id"] for q in self.questions if q["family"] == fam]
                for fam in ("corridor_volume_failed", "failure_share_by", "status_counts")
            },
            "note": "rfi and break pools span the whole window; stuck and reroute pools are monitoring-window cases",
        }

    # ---- run ----
    def run(self) -> None:
        self.build_users()
        self.build_episodes()
        self.build_payments()
        self.run_lifecycles()
        self.plant()
        self.build_events_and_ledger()
        self.orphans()
        self.build_passes()
        self.build_profiles()
        self.build_degradation()
        self.build_rfis()
        self.build_stuck()
        self.build_reroutes()
        self.build_reporting()
        self.questions = [q for q in self.questions if q]
        self.count_hard()


class OracleDisagreement(RuntimeError):
    """The generator's own answer to a question differs from the rule function's over the tables."""


def rule_params(q: dict[str, Any]) -> dict[str, Any]:
    """A question instance as `glossary_answer` reads it: the country, the as-of time and the
    window, times as ISO UTC strings, and the breakdown for a failure share."""
    win = q["window"]
    window: dict[str, Any] = (
        {"kind": "week", "week": win["week"]}
        if win["kind"] == "week"
        else {"kind": "range", "start": iso_utc(win["start"]), "end": iso_utc(win["end"])}
    )
    params: dict[str, Any] = {"country": q["country"], "as_of": iso_utc(q["as_of"]), "window": window}
    if q["family"] == "failure_share_by":
        params["by"] = win["by"]
    return params


def _corr_key(name: str) -> str:
    if name.startswith("summary_"):
        return name[len("summary_") :]
    if name.startswith("attempts_"):
        return name[len("attempts_") :]
    if name == "failed_with_returns_usd" or name == "failed_without_cancelled_usd":
        return "failed_usd"
    if name == "local_time_week_volume_usd":
        return "volume_usd"
    if name == "returns_as_failed_n":
        return "n_failed"
    raise KeyError(name)


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[
        :16
    ]


SCHEMA = """
CREATE TABLE world_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE platforms (platform_id TEXT PRIMARY KEY, name TEXT, corridor_mix TEXT);
CREATE TABLE identity_records (user_id TEXT PRIMARY KEY, platform TEXT, name TEXT, items TEXT, contradictions TEXT, template_contradiction INTEGER);
CREATE TABLE providers (name TEXT PRIMARY KEY, coverage TEXT, statement_lag_s INTEGER, webhook_duplicate REAL, webhook_out_of_order REAL, webhook_late REAL, webhook_drop REAL, webhook_invalid_sig REAL, retry_share REAL, fee_schedule TEXT);
CREATE TABLE provider_vocabulary (provider TEXT, kind TEXT, raw TEXT, unified TEXT, PRIMARY KEY (provider, kind, raw));
CREATE TABLE rails (rail TEXT PRIMARY KEY, destination TEXT, clock TEXT, rule TEXT, limit_minor INTEGER, sources TEXT);
CREATE TABLE calendars (destination TEXT, date TEXT, banking_day INTEGER, source TEXT, PRIMARY KEY (destination, date));
CREATE TABLE fx_mid (corridor TEXT, currency TEXT, date TEXT, rate REAL, published TEXT, banking_day INTEGER, source_date TEXT, source TEXT, PRIMARY KEY (corridor, date));
CREATE TABLE fx_markup_model (provider TEXT, corridor TEXT, markup REAL, band TEXT, label TEXT, floor_pct REAL, market_spread_pct REAL, floor_source TEXT, corridor_band TEXT, PRIMARY KEY (provider, corridor));
CREATE TABLE fx_quotes (attempt_id TEXT PRIMARY KEY, corridor TEXT, mid REAL, markup REAL, quote REAL, label TEXT);
CREATE TABLE payments (payment_id TEXT PRIMARY KEY, platform TEXT, end_user TEXT, corridor TEXT, currency TEXT, amount_usd_cents INTEGER, created_at TEXT, latest_attempt_id TEXT, status TEXT, return_status TEXT, provider TEXT, rail TEXT, attempt_count INTEGER, recon_state TEXT);
CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, payment_id TEXT, n INTEGER, corridor TEXT, provider TEXT, rail TEXT, created_at TEXT, submitted_at TEXT, terminal_at TEXT, terminal_status TEXT, assumed_failed_at TEXT, status TEXT, status_source TEXT, provider_reference TEXT, settlement_reference TEXT, failure_code TEXT, failure_reason TEXT, reason_unmapped INTEGER, return_at TEXT, return_reason TEXT, amount_usd_cents INTEGER, amount_dst INTEGER, fee_minor INTEGER, fee_booked INTEGER, fx_quote REAL, client_reference TEXT);
CREATE TABLE events (event_id TEXT PRIMARY KEY, attempt_id TEXT, provider TEXT, provider_event_id TEXT, occurred_at TEXT, raw_status TEXT, unified TEXT, source TEXT, delivered_at TEXT, signature_valid INTEGER, applied INTEGER, apply_result TEXT, reference TEXT, payload TEXT);
CREATE TABLE statements (entry_id TEXT PRIMARY KEY, provider TEXT, attempt_id TEXT, client_reference TEXT, entry_at TEXT, raw_status TEXT, status TEXT, amount INTEGER, fee INTEGER, reference TEXT, failure_code TEXT, return_reference TEXT, return_amount INTEGER);
CREATE TABLE profiles (provider TEXT, corridor TEXT, rail TEXT, count INTEGER, p50 INTEGER, p95 INTEGER, unit TEXT, baseline_failure REAL, PRIMARY KEY (provider, corridor, rail));
CREATE TABLE passes (pass_id TEXT PRIMARY KEY, cadence TEXT, run_at TEXT, lookback_days INTEGER, compared INTEGER, matched INTEGER, match_rate REAL, raised INTEGER, reobserved INTEGER);
CREATE TABLE breaks (break_id TEXT PRIMARY KEY, attempt_id TEXT, kind TEXT, evidence TEXT, evidence_digest TEXT, version INTEGER, superseded INTEGER, found_at TEXT, first_pass TEXT, seen_in_passes TEXT, state TEXT, resolved_at TEXT, resolution TEXT, statement_entry_id TEXT);
CREATE TABLE break_versions (break_id TEXT PRIMARY KEY, attempt_id TEXT, kind TEXT, version INTEGER, evidence_digest TEXT);
CREATE TABLE rfis (rfi_id TEXT PRIMARY KEY, provider TEXT, attempt_id TEXT, end_user TEXT, platform TEXT, received_at TEXT, readable INTEGER, request_type TEXT, raw_reason TEXT, template INTEGER, state TEXT, follow_ups INTEGER, as_of TEXT);
CREATE TABLE incidents (incident_id TEXT PRIMARY KEY, provider TEXT, service TEXT, corridor TEXT, rail TEXT, first_observed TEXT, resolved_at TEXT, episode_id TEXT);
CREATE TABLE degradation_windows (episode_id TEXT PRIMARY KEY, provider TEXT, corridor TEXT, rail TEXT, start_at TEXT, end_at TEXT, failure_share REAL, label TEXT);
CREATE TABLE webhook_incidents (incident_id TEXT PRIMARY KEY, provider TEXT, start_at TEXT, end_at TEXT);
CREATE TABLE corridor_weekly_summary (corridor TEXT, week INTEGER, provider TEXT, rail TEXT, n_payments INTEGER, volume_usd_cents INTEGER, n_failed INTEGER, failed_usd_cents INTEGER, n_settled INTEGER, refreshed_at TEXT, PRIMARY KEY (corridor, week, provider, rail));
CREATE INDEX idx_events_attempt ON events (attempt_id, delivered_at);
CREATE INDEX idx_attempts_payment ON attempts (payment_id, n);
CREATE INDEX idx_attempts_pair ON attempts (provider, corridor, rail, submitted_at);
CREATE INDEX idx_attempts_created ON attempts (created_at);
CREATE INDEX idx_payments_created ON payments (corridor, created_at);
CREATE INDEX idx_statements_attempt ON statements (attempt_id, entry_at);
CREATE INDEX idx_breaks_attempt ON breaks (attempt_id);
"""


@dataclass(frozen=True)
class WorldDigest:
    seed: int
    payments: int
    out: Path
    tables: dict[str, str]
    oracle: str
    context: dict[str, str]
    hard_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "payments": self.payments,
            "tables": self.tables,
            "oracle": self.oracle,
            "context": self.context,
            "hard_counts": self.hard_counts,
        }


def generate(
    seed: int,
    sources: Path | None = None,
    out: Path | None = None,
    *,
    payments: int = DEFAULT_PAYMENTS,
) -> WorldDigest:
    """Generate the world at `seed` into `out` from the frozen sources. Same seed, same bytes."""
    src = load_sources(sources)
    out = Path(out) if out is not None else Path("out") / f"world-{seed}"
    g = Generator(seed, src, payments)
    g.run()
    g.write(out)
    tables, oracle_digest, context_digests = canonical_digests(out)
    return WorldDigest(
        seed=seed,
        payments=payments,
        out=out,
        tables=tables,
        oracle=oracle_digest,
        context=context_digests,
        hard_counts=dict(g.hard_counts),
    )


DEFAULT_WORLD_DIR = "world"
DIGEST_PIN = Path(__file__).resolve().parent / "pin.json"


class WorldDigestMismatch(RuntimeError):
    """A world at the pinned seed does not reproduce the pinned table-by-table digests."""


def ensure_world(out: Path | None = None, pin: Path | None = None, *, regenerate: bool = True) -> dict[str, Any]:
    """Make sure the world exists at `out` with the pinned digests.

    The world is a pure function of the seed and the frozen sources, so a fresh checkout (a
    worktree at a ref included) can regenerate it byte for byte instead of carrying the files.
    Reads the pin (seed, payments, the table, oracle and context digests); if the folder holds a
    world whose canonical digests equal the pin, returns at once; otherwise, when `regenerate`,
    generates the world there and checks it against the pin, raising `WorldDigestMismatch` with
    the differing names when it does not reproduce them.
    """
    out = Path(out) if out is not None else Path(DEFAULT_WORLD_DIR)
    pin = Path(pin) if pin is not None else DIGEST_PIN
    pinned = json.loads(pin.read_text(encoding="utf-8"))

    def check() -> list[str]:
        tables, oracle_digest, context = canonical_digests(out)
        bad = [t for t in sorted(set(tables) | set(pinned["tables"])) if tables.get(t) != pinned["tables"].get(t)]
        if oracle_digest != pinned["oracle"]:
            bad.append("oracle.json")
        bad += [f"context/{n}" for n in sorted(set(context) | set(pinned["context"])) if context.get(n) != pinned["context"].get(n)]
        return bad

    complete = all((out / name).exists() for name in ("world.sqlite", "oracle.json", "context"))
    bad = check() if complete else ["the folder is missing world.sqlite, oracle.json or context"]
    if not bad:
        return {"path": str(out), "regenerated": False, "seed": pinned["seed"], "digests_equal_pin": True}
    if not regenerate:
        raise WorldDigestMismatch(f"no world with the pinned digests at {out}; differs in: {', '.join(bad)}")
    generate(int(pinned["seed"]), None, out, payments=int(pinned.get("payments", DEFAULT_PAYMENTS)))
    bad = check()
    if bad:
        raise WorldDigestMismatch(f"the world generated at {out} differs from the pin in: {', '.join(bad)}")
    return {"path": str(out), "regenerated": True, "seed": pinned["seed"], "digests_equal_pin": True}
