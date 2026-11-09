"""Command line: the stated cost of the sweeps before any spend, and the spend after.

python -m backoffice_agent plan SPEC.toml --sweeps sonnet haiku   what the sweeps will cost, and the cap
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


#: A sweep's name on the command line, and the model it calls.
SWEEPS: dict[str, str] = {"sonnet": SONNET, "haiku": HAIKU, "planted": SONNET}


def plan(spec: Path, sweeps: list[str]) -> int:
    doc = tomllib.loads(spec.read_text(encoding="utf-8"))
    actions = doc["axes"]["action"]["values"]
    slots = doc["axes"]["case"]["values"]
    reps = int(doc["replicates"])
    total = 0.0
    n = len(actions) * len(slots) * reps
    print(f"plan  {len(sweeps)} sweeps of {n:,} episodes each: "
          f"{len(actions)} action types x {len(slots)} case{'s' if len(slots) > 1 else ''} x {reps} replicates")
    for name in sweeps:
        model = SWEEPS[name]
        est = sum(PER_CASE_USD[model][a] * len(slots) * reps for a in actions)
        total += est
        print(f"  {name}  {model}  about ${est:.2f}")
    spent = ledger_total()
    print(f"  all  about ${total:.2f}; already spent ${spent:.2f} ({ledger_path()})")
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
    pl = sub.add_parser("plan", help="what the sweeps will cost, before any spend")
    pl.add_argument("spec", type=Path)
    pl.add_argument("--sweeps", nargs="+", choices=sorted(SWEEPS), required=True)
    sub.add_parser("spend", help="what the spend ledger holds, by model")
    args = ap.parse_args(argv)
    return plan(args.spec, args.sweeps) if args.cmd == "plan" else spend()


if __name__ == "__main__":
    sys.exit(main())
