"""Reading a generated world: `World.open`, snapshots as of a time, and the oracle.

Reading never writes: the database is opened read-only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import HOUR, T0, from_iso_utc, iso_utc
from .providers import TERMINAL
from .rules import PairState, evaluate_degraded, glossary_answer


@dataclass(frozen=True)
class CorrectAction:
    case_id: str
    action_type: str
    as_of: int
    correct: dict[str, Any]
    hard: tuple[str, ...]
    detail: dict[str, Any]


@dataclass(frozen=True)
class CorrectAnswer:
    instance_id: str
    family: str
    as_of: int
    correct: dict[str, Any]
    planted_wrong_values: dict[str, Any]
    hard: tuple[str, ...]
    detail: dict[str, Any]


class Oracle:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def case(self, case_id: str) -> CorrectAction:
        c = self._data["cases"][case_id]
        return CorrectAction(
            case_id=case_id,
            action_type=c["action_type"],
            as_of=int(c["as_of"]),
            correct=c["correct"],
            hard=tuple(c.get("hard", [])),
            detail=c,
        )

    def question(self, instance_id: str) -> CorrectAnswer:
        q = self._data["questions"][instance_id]
        return CorrectAnswer(
            instance_id=instance_id,
            family=q["family"],
            as_of=int(q["as_of"]),
            correct=q["correct"],
            planted_wrong_values=q["planted_wrong_values"],
            hard=tuple(q.get("hard", [])),
            detail=q,
        )

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


class Snapshot:
    """The world's state as of a time, read from the emitted tables."""

    def __init__(self, world: World, t: int) -> None:
        self.world = world
        self.t = t
        self.t_iso = iso_utc(t)

    def _q(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cur = self.world.conn.execute(sql, params or {})
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def attempt_status(self, attempt_id: str) -> tuple[str, str, int, str | None, str | None]:
        """(status, source, last_event_at, reference, failure_code) of our ledger at the snapshot
        time, rebuilt from the events table and the attempt row, under the ledger's rules: a
        terminal status from a webhook or a poll is never overwritten, an operator may correct an
        assumed one, and an attempt whose poll failed is assumed failed 48 hours after submission."""
        row = self._q("SELECT * FROM attempts WHERE attempt_id = :a", {"a": attempt_id})[0]
        status, source, last, ref, code = "created", "system", from_iso_utc(row["created_at"]), None, None
        rank = {"created": 0, "submitted": 1, "processing": 2, "settled": 3, "failed": 3, "cancelled": 3}
        evs = self._q(
            "SELECT * FROM events WHERE attempt_id = :a AND delivered_at IS NOT NULL AND delivered_at <= :t "
            "ORDER BY delivered_at, event_id",
            {"a": attempt_id, "t": self.t_iso},
        )
        poll_failed_at: int | None = None
        for ev in evs:
            if ev["apply_result"] == "poll_failed":
                poll_failed_at = from_iso_utc(ev["delivered_at"])
                continue
            if not ev["applied"]:
                continue
            payload = json.loads(ev["payload"] or "{}")
            if ev["source"] == "operator":
                if "status" in payload:
                    status, source, last = payload["status"], "operator", from_iso_utc(ev["delivered_at"])
                    ref = payload.get("reference") or ref
                    code = payload.get("failure_code")
                continue
            if ev["unified"] == "return":
                continue
            if status in TERMINAL:
                continue
            if rank.get(ev["unified"], 0) <= rank.get(status, 0) and status != "created":
                continue
            status, source, last = ev["unified"], ev["source"], from_iso_utc(ev["delivered_at"])
            ref = ev["reference"] or ref
            code = payload.get("failure_code")
        if (
            status not in TERMINAL
            and poll_failed_at is not None
            and row["submitted_at"]
            and self.t >= from_iso_utc(row["submitted_at"]) + 48 * HOUR
        ):
            return "failed", "timeout_assumed", last, ref, "assumed_failed_no_answer"
        return status, source, last, ref, code

    def payments(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT * FROM payments WHERE created_at <= :t ORDER BY payment_id", {"t": self.t_iso}
        )

    def attempts(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT * FROM attempts WHERE created_at <= :t ORDER BY attempt_id", {"t": self.t_iso}
        )

    def events(self, attempt_id: str) -> list[dict[str, Any]]:
        return self._q(
            "SELECT * FROM events WHERE attempt_id = :a AND (delivered_at IS NULL OR delivered_at <= :t) ORDER BY event_id",
            {"a": attempt_id, "t": self.t_iso},
        )

    def statement(self, provider: str) -> list[dict[str, Any]]:
        """The provider's statement as of the snapshot time: the latest entry per attempt (and orphans)."""
        rows = self._q(
            "SELECT * FROM statements WHERE provider = :p AND entry_at <= :t ORDER BY attempt_id, entry_at, entry_id",
            {"p": provider, "t": self.t_iso},
        )
        return rows

    def breaks(self, open_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM breaks WHERE found_at <= :t"
        if open_only:
            sql += " AND state IN ('open', 'escalated')"
        return self._q(sql + " ORDER BY break_id", {"t": self.t_iso})

    def rfis(self, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rfis WHERE received_at <= :t"
        params: dict[str, Any] = {"t": self.t_iso}
        if state is not None:
            sql += " AND state = :s"
            params["s"] = state
        return self._q(sql + " ORDER BY rfi_id", params)

    def profiles(self) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM profiles ORDER BY provider, corridor, rail")

    def trailing_counts(self, provider: str, corridor: str, rail: str) -> tuple[int, int]:
        rows = self._q(
            "SELECT terminal_status, terminal_at, submitted_at FROM attempts WHERE provider = :p AND corridor = :c AND rail = :r "
            "AND submitted_at >= :lo AND submitted_at < :t",
            {
                "p": provider,
                "c": corridor,
                "r": rail,
                "lo": iso_utc(self.t - 24 * HOUR),
                "t": self.t_iso,
            },
        )
        n = len(rows)
        f = 0
        for r in rows:
            if r["terminal_status"] == "failed":
                f += 1
        return n, f

    def degraded_pairs(self) -> list[dict[str, Any]]:
        out = []
        for prof in self.profiles():
            n, f = self.trailing_counts(prof["provider"], prof["corridor"], prof["rail"])
            if evaluate_degraded(n, f, prof["baseline_failure"]):
                out.append(
                    {
                        "provider": prof["provider"],
                        "corridor": prof["corridor"],
                        "rail": prof["rail"],
                        "attempts_24h": n,
                        "failures_24h": f,
                        "baseline": prof["baseline_failure"],
                    }
                )
        return out

    def pair_states(self, corridor: str) -> list[PairState]:
        out = []
        for prof in self.profiles():
            if prof["corridor"] != corridor:
                continue
            n, f = self.trailing_counts(prof["provider"], prof["corridor"], prof["rail"])
            out.append(
                PairState(
                    prof["provider"],
                    prof["rail"],
                    evaluate_degraded(n, f, prof["baseline_failure"]),
                    prof["p95"],
                )
            )
        return out

    def incidents(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT * FROM incidents WHERE first_observed <= :t ORDER BY incident_id",
            {"t": self.t_iso},
        )

    def glossary_answer(self, family: str, params: dict[str, Any]) -> dict[str, Any]:
        return glossary_answer(self.world.conn, family, {**params, "as_of": self.t_iso})


class World:
    def __init__(self, path: Path, conn: sqlite3.Connection, oracle: Oracle) -> None:
        self.path = path
        self.conn = conn
        self.oracle = oracle

    @classmethod
    def open(cls, path: Path | str) -> World:
        path = Path(path)
        db = path / "world.sqlite"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        oracle = Oracle(json.loads((path / "oracle.json").read_text(encoding="utf-8")))
        return cls(path, conn, oracle)

    def close(self) -> None:
        self.conn.close()

    @property
    def context_dir(self) -> Path:
        return self.path / "context"

    @property
    def now(self) -> int:
        return T0

    def as_of(self, t: int) -> Snapshot:
        return Snapshot(self, t)

    def meta(self) -> dict[str, str]:
        return {k: v for k, v in self.conn.execute("SELECT key, value FROM world_meta")}

    def cases(self, action_type: str, hard_only: bool = False) -> list[str]:
        ids = self.oracle.raw["pools"][action_type]
        if not hard_only:
            return list(ids)
        return [i for i in ids if self.oracle.raw["cases"][i].get("hard")]

    def questions(self, family: str) -> list[str]:
        return list(self.oracle.raw["pools"]["questions"][family])

    def tables(self) -> list[str]:
        return sorted(
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
