# boreal.work for Hermes

**Give Hermes verified human hands.** Some steps no agent can do online: confirm a supplier's warehouse is real before a deposit, check what a product sells for on a shelf today, photograph a building site, count who stops at a display. This plugin lets Hermes hire a verified person on [boreal.work](https://boreal.work) for that step, pay only when the result passes review, and bring the verdict back to you.

It runs against **live boreal.work** by default (PayPal, real money), behind a spend guard you set.

## What it adds to Hermes

| | What it does |
| --- | --- |
| **12 tools** | `boreal_connect`, `boreal_check_link`, `boreal_account`, `boreal_quote`, `boreal_post_task`, `boreal_fund_task`, `boreal_list_tasks`, `boreal_get_task`, `boreal_get_submission`, `boreal_review`, `boreal_accept`, `boreal_cancel`: the whole task loop, natively, with no MCP setup. |
| **Spend guard** (`pre_tool_call`) | Before Hermes funds a task, the guard reads the task's real total and checks it against your limits: blocked over the per-task or daily cap, and sent to you for approval above a threshold. It also blocks USDC payments made from a terminal that aren't paying a boreal.work task (no `boreal:<taskId>` memo). A skill can only ask Hermes to behave; the guard enforces it. |
| **Verdict digest** (`pre_llm_call`) | On the first turn of each session, Hermes is told which of its tasks finished or need its review since it last looked, so results come back without anyone asking. |
| **`/boreal`** | Your account, today's spend against your limit, and open tasks. |

## Install

```bash
hermes plugins install raldblox/hermes-boreal
hermes plugins enable borealwork
```

Then just ask Hermes for something that needs a person on the spot. The first time, it calls `boreal_connect` and gives you a link: log in with PayPal, set its limits, approve, and connect PayPal once so it can fund tasks itself. Its key is kept in this plugin's private state (or set `BOREAL_API_KEY` yourself).

## Settings

Under `plugins.entries.borealwork.settings` in `config.yaml`, or the Desktop's Plugins page:

| Setting | Default | Meaning |
| --- | --- | --- |
| `base_url` | `https://boreal.work` | Live money. Use `https://testnet.boreal.work` to try it with the PayPal sandbox. |
| `max_task_usd` | `10` | Most Hermes may fund for one task (pay plus fee). |
| `max_daily_usd` | `50` | Most Hermes may fund in a day. |
| `approve_over_usd` | `5` | Above this, funding a task asks you first. |
| `verdict_digest` | `true` | Tell Hermes what finished at the start of each session. |
| `api_key` | (none) | Optional `BOREAL_API_KEY`; otherwise `boreal_connect` gets one. |

These limits sit on top of the ones boreal.work itself enforces for your agent (set when you approve it).

## How the money works

Posting only quotes a task. Funding holds the money (a PayPal authorization, not a charge). boreal.work screens the task against its [rulebook](https://boreal.work/rules), a verified person does it, and boreal.work's reviewer (or Hermes, if you choose) judges the result against your criteria. A pass pays the worker; a fail returns your money less the review part of the fee; an expired or cancelled task returns all of it. Every fail cites a rule by ID, and either side can dispute once to a panel with a person on it.

## What it touches (catalog rule 13)

- **Network:** HTTPS to the configured boreal.work site only (`/api/v1/*`), from tool calls, from the spend guard (one task read before funding), and from the digest (one task list on a session's first turn).
- **Stored credentials:** the boreal.work agent key collected by `boreal_check_link`, in this plugin's own state under `<HERMES_HOME>/plugin-data/`. Nothing else.
- **No shell commands, no background processes, no self-updates, no compiled code.** The guard reads terminal commands Hermes is about to run, only to block untagged payments.

## Develop

```bash
python -m unittest discover -s tests
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

Standard library only. MIT licence.
