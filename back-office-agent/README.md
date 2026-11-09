# Audit a back-office agent with Prove and Trace

An operations agent on Claude works cases in a synthetic payments back office: compliance
requests (RFIs), reconciliation breaks, late payouts and failing rails. Every case has a correct
answer the world's generator knows, so every pick the agent makes is scored by code, never by a
model. Prove sweeps the agent over twenty fixed cases on Claude Sonnet 5.5, then again on Claude
Haiku 5.5 on the same seeds, and compares the two runs condition by condition. Trace reads the
same two runs and says where in the episode failures stop, and whether the cheaper model failed
more or less often across all twenty cases at once.

The world is synthetic. The transactions, end users, cases and the six providers are invented;
the rails' clocks, the destinations' banking calendars and the FX fixings copy public rules and
official series, frozen under `src/backoffice_world/sources/` with their URLs, the dates read
and digests. It is a walkthrough, not anyone's real system.

## What you need

- Python 3.11 or later, on macOS or Linux.
- An Anthropic API key in `ANTHROPIC_API_KEY`. Every model call is yours.
- About $16 of model spend for both sweeps (`python -m backoffice_agent plan` prints it before
  anything runs), under a $20 cap the agent enforces itself.
- For the Trace steps, Poisson Trace 0.1.0rc2. Trace is a pre-release and is not on PyPI yet:
  the `poisson-trace` there today is a placeholder. Ask work@poissonlabs.ai for the wheel; once
  Trace is public, `pip install poisson-trace` replaces it.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
python -m backoffice_world ensure world      # generates the world (about 6 minutes) and checks its digests
BACKOFFICE_WORLD=world pytest tests          # keyless: the oracle's own pick scores right on every case (pip install pytest)
python -m backoffice_agent plan              # the stated cost, before any spend
poisson-prove sweep --dry-run                # Prove's plan and its design limit
poisson-prove sweep --yes --max-mde 0.99
poisson-prove sweep --yes --max-mde 0.99 --target backoffice_agent.agent:rollout_haiku --pair-with latest
poisson-prove diff
poisson-prove report --x case --y action
python -m backoffice_agent spend             # what the two sweeps cost
```

Then Trace, in an environment of its own: Trace 0.1.0rc2 reads Prove through an exact pin,
`poisson-prove==0.1.3`, and reads the store Prove 0.2.2 wrote as it is.

```bash
python3 -m venv trace-env
trace-env/bin/pip install ./poisson_trace-0.1.0rc2-py3-none-any.whl
trace-env/bin/poisson-trace where "back-office-agent DATE a" --store prove-runs --event wrong --by step
trace-env/bin/poisson-trace compare "back-office-agent DATE a" "back-office-agent DATE b" --store prove-runs --event wrong --by step --max-mde-hr 1.5
trace-env/bin/poisson-trace fix-check "back-office-agent DATE a" "back-office-agent DATE b" --store prove-runs --event wrong --inputs all --max-mde 0.10
```

`DATE` is the day the runs were made; `poisson-prove runs` lists their names. Trace refuses an
event a run does not carry, so name the failures your runs hold (`poisson-trace runs --store
prove-runs` lists them).

## What is here

| Path | What it is |
|---|---|
| `src/backoffice_world/` | the world generator: a seeded payments back office (payments, attempts, statements, reconciliation passes and breaks, RFIs, identity records, rail health), its oracle and its context documents. `python -m backoffice_world ensure world` regenerates it byte for byte and checks it against `pin.json` |
| `src/backoffice_agent/agent.py` | the agent's loop, its two rollouts (`rollout_sonnet`, `rollout_haiku`), the fixed cases, and the spend ledger with its cap |
| `src/backoffice_agent/tools.py` | six read-only tools and the action tools; no tool writes the world or moves money |
| `src/backoffice_agent/scorer.py`, `notes.py`, `facts.py` | the scorer: `right`, `wrong`, `harmful` or `no_action`, from the pick and the world's own answer |
| `src/backoffice_agent/prompts/` | the system prompt and the four task messages |
| `prove.toml` | the sweep spec: four action types by five cases, 20 replicates each |
| `tests/` | the keyless check, with a scripted client that plays the world's own answer |

## How the agent is wrapped for Prove

`rollout_sonnet(cfg, seed)` runs the case `cfg` names (an action type and a slot, so a
condition always runs the same case) and returns `poisson_prove.Episode` with the outcome, the
tool calls it made as `t`, and the case's id in `meta`. Each tool call is recorded with
`poisson_prove.record` as a step named after the tool, which is what Trace reads as a step.
`rollout_haiku` is the same agent, prompt and tools on the cheaper model: the change under test,
one rollout function per model, as Prove's recipe for a model change does it.

The Anthropic API takes no seed and these models take no temperature, so replicates of a case
vary with the model's own sampling. Pairing still matches replicate i of a case in one run with
replicate i in the other.

## Spend

Each model call is priced from the usage the API returns, at the rates in `agent.PRICES`, and
appended to `spend.jsonl`. Before every call the ledger's total is checked against
`BACKOFFICE_CAP_USD` ($20 by default); at the cap an episode makes no call and raises, and Prove
stores it as an errored episode.

## Licence

MIT, as the repository's `LICENSE`.
