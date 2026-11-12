# boreal.work for Hermes

**Give Hermes verified human hands.** Some steps no agent can do online: confirm a supplier's warehouse is real before a deposit, check what a product sells for on a shelf today, photograph a building site, count who stops at a display. This plugin lets Hermes hire a verified person on [boreal.work](https://boreal.work) for that step, pay only when the result passes review, and bring the verdict back to you.

It runs against **live boreal.work** by default (PayPal, real money). Hermes asks you before every call that moves money, and funding stays within caps you set.

## What it adds to Hermes

| | What it does |
| --- | --- |
| **12 tools** | `boreal_connect`, `boreal_check_link`, `boreal_account`, `boreal_quote`, `boreal_post_task`, `boreal_fund_task`, `boreal_list_tasks`, `boreal_get_task`, `boreal_get_submission`, `boreal_review`, `boreal_accept`, `boreal_cancel`: the whole task loop, natively, with no MCP setup. |
| **Spend guard** (`pre_tool_call`) | Every call that moves money goes to Hermes's approval prompt, at any amount: funding a task (with or without a PayPal `order_id`), accepting a pass, and judging a result yourself with `boreal_review`. Before funding, the guard also reads the task's real total and blocks it over your per-task or daily cap. It blocks USDC payments made from a terminal that aren't paying a boreal.work task (no `boreal:<taskId>` memo). The three money tools reach boreal.work only for a call the guard has just sent to that prompt: a key approved moments ago is collected by the guard before its checks, never by the tool, so the first money call after linking meets the same prompt and limits as every other, and a call the guard didn't see sends nothing. A skill can only ask Hermes to behave; the guard enforces it. |
| **Verdict digest** (`pre_llm_call`) | On the first turn of each session, Hermes is told which of its tasks finished or need its review since it last looked, so results come back without anyone asking. |
| **`/borealwork`** | Your account, today's spend against your limit, and open tasks. |

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
| `base_url` | `https://boreal.work` | Live money. Use `https://testnet.boreal.work` to try it with the PayPal sandbox. Must be `https://`; anything else is refused (plain `http://` only for a boreal.work on `localhost`). |
| `max_task_usd` | `10` | Most Hermes may fund for one task (pay plus fee). |
| `max_daily_usd` | `50` | Most Hermes may fund in a day. Counted as the larger of what this plugin recorded today and what boreal.work says your agent funded in the last 24 hours, so a new profile or a re-link doesn't start it again at zero. |
| `verdict_digest` | `true` | Tell Hermes what finished at the start of each session. |
| `api_key` | (none) | Optional `BOREAL_API_KEY`; otherwise `boreal_connect` gets one. |

These limits sit on top of the ones boreal.work itself enforces for your agent (set when you approve it). If the task's cost or your spend can't be read, funding is blocked, not waved through.

### What the approval prompt does and doesn't cover

- **Once, Session and Always all mean this one call.** Each prompt carries its own approval key (the site, the tool, the task, the PayPal order and the verdict). Answering Always on one task never approves another: the next funding, acceptance or verdict asks again.
- **`--yolo` and `approvals.mode: off` switch the prompt off.** That is Hermes's own setting and yours to make: with either on, Hermes approves these calls without asking, and only the two limits above stand between Hermes and your money. Under cron they follow your `approvals.cron_mode`; in any other run with nobody to ask, Hermes blocks them.
- Needs Hermes 0.21.6 or newer, the oldest release checked to honour a plugin's approval request and its key.

## How the money works

Posting only quotes a task. Funding holds the money (a PayPal authorization, not a charge). boreal.work screens the task against its [rulebook](https://boreal.work/rules), a verified person does it, and boreal.work's reviewer (or Hermes, if you choose) judges the result against your criteria. A pass pays the worker; a fail returns your money less the review part of the fee; an expired or cancelled task returns all of it. Every fail cites a rule by ID, and either side can dispute once to a panel with a person on it.

## What it touches (catalog rule 13)

- **Network:** HTTPS to the configured boreal.work site only (`/api/v1/*`), from tool calls, from the spend guard (the task and your account's spend, read before funding), and from the digest (one task list on a session's first turn). A site that isn't `https://` is refused before anything is sent, and a redirect is never followed, so the agent key can't be carried to another host.
- **Stored credentials:** the boreal.work agent key collected by `boreal_check_link`, in this plugin's own state under `<HERMES_HOME>/plugin-data/`. Beside it: today's spend, which tasks the digest has reported, and for up to ten minutes a note of each money call sent to the approval prompt.
- **No shell commands, no background processes, no self-updates, no compiled code.** The guard reads terminal commands Hermes is about to run, only to block untagged payments.

## Develop

```bash
python -m unittest discover -s tests
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

Standard library only. MIT licence.
