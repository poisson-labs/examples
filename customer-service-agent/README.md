# Set up Prove on a customer-service agent with your coding agent

OpenAI's public airline customer-service demo, [`openai/openai-cs-agents-demo`](https://github.com/openai/openai-cs-agents-demo),
stands in for your own repository: a triage agent on the OpenAI Agents SDK that hands a customer
to five specialists, behind two guardrails. A coding agent, given the one prompt Prove writes,
reads the repository and writes the two files Prove needs. You review them, run the agent before
and after one change, and compare.

This folder holds what that took on one run, written up with its stored output at
[poissonlabs.ai/docs/examples/set-up-with-your-coding-agent](https://poissonlabs.ai/docs/examples/set-up-with-your-coding-agent/).

| File | What it is |
|---|---|
| `model-step.diff` | Points the agent's model at Claude Haiku 5.5, through the Agents SDK's LiteLLM route |
| `as-written/prove_rollout.py`, `as-written/prove.toml` | The two files exactly as the coding agent wrote them |
| `transcript/coding-agent.jsonl` | The coding agent's whole session: Claude Code 2.1.329 on Claude Sonnet 5.5, run headless with `--output-format stream-json` |
| `review-step.diff` | The review: what was changed in the two files before anything ran for real, and why (below) |
| `reviewed/prove_rollout.py`, `reviewed/prove.toml` | The two files after the review, as the sweeps ran them |
| `offer-compensation.diff` | The change under test: one line in the Flight Information Agent's instructions |

A coding agent given the same prompt will write different files. The review list is what to
check in yours.

## What you need

- Python 3.11 or later, on macOS or Linux.
- An Anthropic API key in `ANTHROPIC_API_KEY`, for the coding agent and for the sweeps. Nothing
  before step 6 calls the agent's model, and steps 1 to 3 and 5 need no key.
- A coding agent: Claude Code, Codex or Cursor. To follow the rest without one, copy the files
  as written (step 4).

## 1. Clone the stand-in at the pinned commit

```bash
git clone https://github.com/poisson-labs/examples
git clone https://github.com/openai/openai-cs-agents-demo
cd openai-cs-agents-demo
git checkout bd7bfca0f5abf50529370814c3e7c88542011925
```

## 2. Point the agent at Claude

```bash
git apply ../examples/customer-service-agent/model-step.diff
python3 -m venv .venv && source .venv/bin/activate
pip install poisson-prove==0.4.0 "openai-agents[litellm]==0.25.0" litellm==1.110.2 -r python-backend/requirements.txt
```

With no OpenAI key set, the Agents SDK skips uploading its traces and prints
`OPENAI_API_KEY is not set, skipping trace export` once per conversation. No OpenAI call is made.

## 3. Write the skill and the prompt

```bash
poisson-prove init --agent
```

## 4. Give your coding agent the prompt

```text
Use the poisson-prove-setup skill in this repository: set Poisson Prove up for me to review, then stop.
```

The stored run gave it to Claude Code headless, with edits allowed and one command allowed:

```bash
claude -p "$(cat .claude/skills/poisson-prove-setup/prompt.txt)" --model claude-sonnet-5-5 \
  --permission-mode acceptEdits --allowedTools "Bash(poisson-prove sweep --dry-run)" \
  --output-format stream-json --verbose > coding-agent.jsonl
```

Without a coding agent, use what this one wrote:

```bash
cp ../examples/customer-service-agent/as-written/prove_rollout.py ../examples/customer-service-agent/as-written/prove.toml .
poisson-prove sweep --dry-run
```

## 5. Review the two files

Checked against Prove's [Defining outcomes](https://github.com/poisson-labs/prove/blob/v0.4.0/docs/defining-outcomes.md):

- **`error` took every exception.** A rate limit, an overloaded provider or a dropped connection
  ended up in the same label as the agent failing, so a bad afternoon at the provider would read
  as the agent getting worse. Provider errors now end as `api_error`, counted as neither a
  failure nor a success. LiteLLM raises every provider error as a subclass of `openai.APIError`.
- **No outcome could see the change.** The change is about compensation, and every normal
  ending was `completed`. A run in which the agent opened a compensation case
  (`issue_compensation` sets `compensation_case_id`) now ends `compensated`.
- **`completed` was declared a success.** It means the conversation ended, not that the customer
  was helped, and with `compensated` split out of it, a rise in compensation would read as fewer
  successes. Both are now counted as neither; the failures are `max_turns` and `error`.
- **The `model` axis compared Claude Haiku 5.5 with Claude Sonnet 5.5.** The change under test is
  not a model change, and Sonnet's half of the runs was about 95% of their cost. Removed.
- **`replicates = 221`.** That was the number for the first sweep at 16 conditions. The sweep
  after the change is compared pair by pair and is checked again; at 8 conditions it needs 190.

The diff applies to the files as written. On files your own coding agent wrote, make the same
checks by hand.

```bash
git apply ../examples/customer-service-agent/review-step.diff
poisson-prove sweep --dry-run
```

## 6. Run the agent before the change

This one spends: 1,520 conversations on Claude Haiku 5.5.

```bash
export ANTHROPIC_API_KEY=...
poisson-prove sweep --yes --name before
```

## 7. Make the change, and run it again on the same cases

```bash
git apply ../examples/customer-service-agent/offer-compensation.diff
poisson-prove sweep --yes --pair-with before --name after
```

## 8. Compare

```bash
poisson-prove diff before after
poisson-prove report before after --x case --y scenario
```

## Counting what it spends

Prove's plan prints `$0 (local)` for a sweep on your machine: that is the cost of running Prove,
not of the model calls each rollout makes. The stored run counted those with `spend/`: a
`sitecustomize.py` that, when its folder is on `PYTHONPATH`, writes the token usage of every
model call LiteLLM makes to a ledger, and `price.py`, which prices the ledgers at Anthropic's
published rates.

```bash
PYTHONPATH=../examples/customer-service-agent/spend CS_METER_LEDGER=ledger-before.jsonl \
  poisson-prove sweep --yes --name before
python3 ../examples/customer-service-agent/spend/price.py ledger-before.jsonl
```

On the stored run (2026-11-21), the coding agent cost $0.20 and the agent's own calls $2.67:
$0.03 for a three-conversation check that the agent runs on Claude, then $1.31 and $1.33 for the
two sweeps of 1,520 conversations each on Claude Haiku 5.5. The ledgers are stored with the page.

MIT licensed, as is the stand-in; see `LICENSE` at the repository root.
