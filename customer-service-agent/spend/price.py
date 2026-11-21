"""Price meter ledgers at Anthropic's published per-token rates (USD per million tokens).

    python price.py LEDGER [LEDGER ...]

Prints one line per ledger, one per model over all of them, and the total.
"""

import json
import sys
from collections import defaultdict

RATES = {  # input, output, cache read, cache write (5-minute)
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
}


def base(model: str) -> str:
    return model.split("/")[-1] if model else "?"


tot = defaultdict(lambda: {"calls": 0, "errors": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0})
for path in sys.argv[1:]:
    before = sum(t["usd"] for t in tot.values())
    calls = sum(t["calls"] for t in tot.values())
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        m = base(r.get("model"))
        t = tot[m]
        if not r.get("ok"):
            t["errors"] += 1
            continue
        t["calls"] += 1
        cr, cw = r.get("cache_read", 0), r.get("cache_write", 0)
        # LiteLLM's prompt_tokens for Anthropic includes cache reads and writes.
        plain = max(r.get("input", 0) - cr - cw, 0)
        ri, ro, rcr, rcw = RATES[m]
        t["input"] += plain
        t["output"] += r.get("output", 0)
        t["cache_read"] += cr
        t["cache_write"] += cw
        t["usd"] += (plain * ri + r.get("output", 0) * ro + cr * rcr + cw * rcw) / 1e6
    print(json.dumps({"ledger": path, "calls": sum(t["calls"] for t in tot.values()) - calls,
                      "usd": round(sum(t["usd"] for t in tot.values()) - before, 6)}))

grand = 0.0
for m, t in sorted(tot.items()):
    grand += t["usd"]
    print(json.dumps({"model": m, **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in t.items()}}))
print(json.dumps({"total_usd": round(grand, 6)}))
