# Poisson Labs examples

This repository holds the code each Poisson Labs example needs, and any mock world it runs in,
one folder per example. Examples that need only the tools (a pip install and the tools' own
commands) keep no code here, and a research post's code stays in that study's own repository.
Every example is written up, with the output of a stored run, on
[poissonlabs.ai/docs/examples](https://poissonlabs.ai/docs/examples/).

| Folder | What it is |
|---|---|
| [`back-office-agent`](back-office-agent/) | An operations agent on Claude in a synthetic payments back office, with Prove and Trace: whether it can move to a cheaper model, and catching a planted regression |

## Install

Each folder installs on its own, with Python 3.11 or later. For the back-office agent, which
installs Prove from PyPI with it:

```bash
git clone https://github.com/poisson-labs/examples
cd examples/back-office-agent
python3 -m venv .venv && source .venv/bin/activate
pip install . pytest
```

## Run one

No key and no spend: generate the synthetic world (about six minutes; it is checked against
the digests the repository pins), then run the keyless check of the agent's wiring.

```bash
python -m backoffice_world ensure world
BACKOFFICE_WORLD=world pytest tests
```

The sweeps call a model on your own key, with their cost stated before they spend; see
[`back-office-agent/README.md`](back-office-agent/README.md).

MIT licensed; see `LICENSE`.
