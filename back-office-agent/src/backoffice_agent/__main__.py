"""Command line: the stated cost of the sweeps before any spend, and the spend after.

python -m backoffice_agent plan [prove.toml]   what the two sweeps will cost, and the cap
python -m backoffice_agent spend               what the spend ledger holds, by model
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

from .agent import HAIKU, SONNET, cap_usd, ledger_path, ledger_total

#: Dollars per case by action type, measured on an earlier run of this same agent, prompt and
#: tools: 150 cases of each type on each model, priced at the rates in agent.PRICES.
PER_CASE_USD: dict[str, dict[str, float]] = {
    SONNET: {"rfi": 0.0477, "break": 0.0255, "stuck": 0.0308, "reroute": 0.0450},
    HAIKU: {"rfi": 0.0016, "break": 0.0013, "stuck": 0.0016, "reroute": 0.0023},
}


def plan(spec: Path) -> int:
    doc = tomllib.loads(spec.read_text(encoding="utf-8"))
    actions = doc["axes"]["action"]["values"]
    slots = doc["axes"]["case"]["values"]
    reps = int(doc["replicates"])
    total = 0.0
    print(f"plan  two sweeps of {len(actions) * len(slots) * reps:,} episodes each: "
          f"{len(actions)} action types x {len(slots)} cases x {reps} replicates")
    for model in (SONNET, HAIKU):
        est = sum(PER_CASE_USD[model][a] * len(slots) * reps for a in actions)
        total += est
        print(f"  {model}  about ${est:.2f}")
    spent = ledger_total()
    print(f"  both  about ${total:.2f}; already spent ${spent:.2f} ({ledger_path()})")
    print(f"  cap  ${cap_usd():.2f}: at the cap an episode makes no call and raises (BACKOFFICE_CAP_USD)")
    print("  per case: measured on an earlier run of this agent, by action type")
    return 0


def spend() -> int:
    p = ledger_path()
    by_model: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0.0])
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            m = by_model[r["model"]]
            m[0] += 1
            m[1] += r["input_tokens"]
            m[2] += r["output_tokens"]
            m[3] += r["usd"]
    total = sum(m[3] for m in by_model.values())
    print(f"spent  ${total:.2f} of a ${cap_usd():.2f} cap, from {p}")
    for model, (calls, tin, tout, usd) in sorted(by_model.items()):
        print(f"  {model}  ${usd:.2f}  {calls:,} calls, {tin:,} tokens in, {tout:,} out")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backoffice_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="what the two sweeps will cost, before any spend")
    pl.add_argument("spec", type=Path, nargs="?", default=Path("prove.toml"))
    sub.add_parser("spend", help="what the spend ledger holds, by model")
    args = ap.parse_args(argv)
    return plan(args.spec) if args.cmd == "plan" else spend()


if __name__ == "__main__":
    sys.exit(main())
