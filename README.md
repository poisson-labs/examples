# Poisson Labs examples

Worked examples on the Poisson Labs tools, each in a folder of its own. Each one is written up,
with the output of a stored run, on [poissonlabs.ai/docs/examples](https://poissonlabs.ai/docs/examples/).

| Folder | What it does | Tools |
|---|---|---|
| [`back-office-agent`](back-office-agent/) | An operations agent on Claude over a synthetic payments back office, two ways: whether it can move to a cheaper model, and catching a planted regression and the step it broke at | Prove, Trace |

## Install

Python 3.11 or later. The example installs Prove from PyPI with it.

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
