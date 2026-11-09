# Audit a back-office agent with Prove and Trace

An operations agent on Claude works cases in a synthetic payments back office: compliance
requests (RFIs), reconciliation breaks, late payouts and failing rails. Every case has a correct
answer the world's generator knows, so every pick the agent makes is scored by code, never by a
model. Two examples run on it, each with its own sweep spec:

- **Can the agent move to a cheaper model?** (`cheaper-model.toml`) Prove sweeps the agent on
  Claude Sonnet 5.5, then on Claude Haiku 5.5 on the same seeds, one fixed case per action type
  at the fewest replicates Prove's power check accepts, and compares the two runs condition by
  condition. Trace reads the same runs.
- **Catch a planted regression** (`planted-regression.toml`). A known-bad change, made on
  purpose: `read_identity_record` times out on every call (`src/backoffice_agent/planted.py`).
  Prove compares the agent before and after on twenty fixed cases; Trace names the step the
  failures now stop at.

The world is synthetic. The transactions, end users, cases and the six providers are invented;
the rails' clocks, the destinations' banking calendars and the FX fixings copy public rules and
official series, frozen under `src/backoffice_world/sources/` with their URLs, the dates read
and digests. It is a walkthrough, not anyone's real system.

## What you need

- Python 3.11 or later, on macOS or Linux.
- An Anthropic API key in `ANTHROPIC_API_KEY`. Every model call is yours.
- About $30 of model spend for each example; `python -m backoffice_agent plan` prints it before
  anything runs, and the agent refuses a call at `BACKOFFICE_CAP_USD` ($20 unless you set it).
- For the Trace steps, Poisson Trace 0.1.0rc2. Trace is a pre-release and is not on PyPI yet:
  the `poisson-trace` there today is a placeholder. Ask work@poissonlabs.ai for the wheel; once
  Trace is public, `pip install poisson-trace` replaces it.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
python -m backoffice_world ensure world      # generates the world (about 6 minutes) and checks its digests
BACKOFFICE_WORLD=world pytest tests          # keyless checks of the wiring (pip install pytest)
```

The cheaper model:

```bash
export BACKOFFICE_CAP_USD=40
python -m backoffice_agent plan cheaper-model.toml --sweeps sonnet haiku
poisson-prove sweep cheaper-model.toml --dry-run
poisson-prove sweep cheaper-model.toml --yes --name sonnet
poisson-prove sweep cheaper-model.toml --yes --target backoffice_agent.agent:rollout_haiku --pair-with sonnet --name haiku
poisson-prove diff sonnet haiku
python -m backoffice_agent spend
```

The planted regression, in a folder of its own so its store and ledger stay apart:

```bash
export BACKOFFICE_CAP_USD=35
python -m backoffice_agent plan planted-regression.toml --sweeps sonnet planted
poisson-prove sweep planted-regression.toml --dry-run
poisson-prove sweep planted-regression.toml --yes --max-mde 0.729 --name before
poisson-prove sweep planted-regression.toml --yes --max-mde 0.729 --target backoffice_agent.planted:rollout_planted --pair-with before --name planted
poisson-prove diff before planted
python -m backoffice_agent spend
```

At twenty replicates Prove's power check refuses its default 0.20: one condition could only be
flagged for a large change. `--max-mde 0.729` is the smallest change Prove itself offers to
accept for the paired sweep; the check stays on at that size. A planted break that stops every
RFI is far larger; a subtle regression would not be seen at this size.

Then Trace, in an environment of its own: Trace 0.1.0rc2 reads Prove through an exact pin,
`poisson-prove==0.1.3`, and reads the store Prove 0.2.2 wrote as it is.

```bash
python3 -m venv trace-env
trace-env/bin/pip install ./poisson_trace-0.1.0rc2-py3-none-any.whl
trace-env/bin/poisson-trace runs --store prove-runs
trace-env/bin/poisson-trace where planted --store prove-runs --event no_action --by step
trace-env/bin/poisson-trace compare before planted --store prove-runs --event no_action --by step --max-mde-hr 1.5
```

Trace refuses an event a run does not carry; `runs` lists the outcomes each run holds.

## What is here

| Path | What it is |
|---|---|
| `src/backoffice_world/` | the world generator: a seeded payments back office (payments, attempts, statements, reconciliation passes and breaks, RFIs, identity records, rail health), its oracle and its context documents. `python -m backoffice_world ensure world` regenerates it byte for byte and checks it against `pin.json` |
| `src/backoffice_agent/agent.py` | the agent's loop, its two rollouts (`rollout_sonnet`, `rollout_haiku`), the fixed cases, and the spend ledger with its cap |
| `src/backoffice_agent/planted.py` | the planted regression: the same agent with `read_identity_record` timing out |
| `src/backoffice_agent/tools.py` | six read-only tools and the action tools; no tool writes the world or moves money |
| `src/backoffice_agent/scorer.py`, `notes.py`, `facts.py` | the scorer: `right`, `wrong`, `harmful` or `no_action`, from the pick and the world's own answer |
| `src/backoffice_agent/prompts/` | the system prompt and the four task messages |
| `cheaper-model.toml` | four action types by one fixed case, 190 replicates each |
| `planted-regression.toml` | four action types by five fixed cases, 20 replicates each |
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
