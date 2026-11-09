"""Command line: generate the world, check it against the pinned digests, or print its digests.

python -m backoffice_world ensure [world]               (generate when missing, check the pin)
python -m backoffice_world generate --seed 2026 --out world [--payments 60000]
python -m backoffice_world digest world
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .digest import canonical_digests, world_digest
from .generate import DEFAULT_PAYMENTS, DEFAULT_WORLD_DIR, ensure_world, generate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backoffice_world")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("ensure", help="generate the world at the pinned seed when missing, and check it byte for byte")
    e.add_argument("path", type=Path, nargs="?", default=Path(DEFAULT_WORLD_DIR))
    e.add_argument("--pin", type=Path, default=None)
    e.add_argument("--no-regenerate", action="store_true")
    g = sub.add_parser("generate", help="generate a world at any seed")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--payments", type=int, default=DEFAULT_PAYMENTS)
    g.add_argument("--sources", type=Path, default=None)
    d = sub.add_parser("digest", help="print a world's table, oracle and context digests")
    d.add_argument("path", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "ensure":
        res = ensure_world(args.path, args.pin, regenerate=not args.no_regenerate)
        state = "generated" if res["regenerated"] else "found"
        print(f"ok  world {state} at {res['path']}, seed {res['seed']}, digest {world_digest(args.path)[:12]}, equal to the pin")
        return 0
    if args.cmd == "generate":
        wd = generate(args.seed, args.sources, args.out, payments=args.payments)
        print(json.dumps(wd.as_dict(), indent=1, sort_keys=True, default=str))
        return 0
    tables, oracle, context = canonical_digests(args.path)
    print(json.dumps({"tables": tables, "oracle": oracle, "context": context}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
