"""Canonical dumps and digests of a generated world.

The sqlite file's bytes depend on the sqlite build, so determinism is pinned on a canonical dump:
every table in name order, every row sorted, serialised as JSON lines, and hashed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def canonical_table_dump(conn: sqlite3.Connection, table: str) -> bytes:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
    lines = [
        json.dumps(dict(zip(cols, r, strict=True)), sort_keys=True, default=str, ensure_ascii=False)
        for r in rows
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def table_names(conn: sqlite3.Connection) -> list[str]:
    return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))


def table_digests(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            t: hashlib.sha256(canonical_table_dump(conn, t)).hexdigest() for t in table_names(conn)
        }
    finally:
        conn.close()


def canonical_digests(out: Path) -> tuple[dict[str, str], str, dict[str, str]]:
    tables = table_digests(out / "world.sqlite")
    oracle = hashlib.sha256((out / "oracle.json").read_bytes()).hexdigest()
    context = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((out / "context").iterdir())
    }
    return tables, oracle, context


def world_digest(out: Path) -> str:
    """One digest over the canonical dump, the oracle and the context documents."""
    tables, oracle, context = canonical_digests(out)
    blob = json.dumps(
        {"tables": tables, "oracle": oracle, "context": context}, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
