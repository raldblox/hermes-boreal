"""boreal.work for Hermes: hire verified people for real-world steps, with a spend guard.

What this plugin adds that a skill can't:
- Native tools for the whole task loop (connect, quote, post, fund, check, review, accept, cancel).
- A spend guard on ``pre_tool_call``: before any boreal.work funding, it checks the task's total
  against the owner's per-task and daily caps set here, and asks the owner to approve anything above
  a threshold. It also blocks USDC payments made through a terminal that don't go to a boreal.work task.
- A verdict digest on ``pre_llm_call``: on a session's first turn, Hermes is told which of its tasks
  finished since it last looked, so results come back without anyone asking.
- ``/borealwork``: the account, today's spend and open tasks, in one line each.

Talks only to the configured boreal.work site (https://boreal.work by default: live PayPal money).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Optional

from . import client, schemas

logger = logging.getLogger(__name__)

TOOLSET = "boreal"
FINISHED = {"final_pass", "final_fail", "refused", "expired", "cancelled"}
NEEDS_YOU = {"submitted", "passed", "failed"}

_ctx: Any = None


# ---------- settings, key and state ----------

def _setting(key: str, default: Any) -> Any:
    try:
        value = _ctx.get_config(key, default=default) if _ctx is not None else default
    except Exception:  # a settings read must never break a tool
        value = default
    return default if value in (None, "") else value


def _base_url() -> str:
    return str(_setting("base_url", "https://boreal.work")).rstrip("/")


def _state_get(key: str, default: Any = None) -> Any:
    try:
        return _ctx.state.get(key, default=default)
    except Exception:
        return default


def _state_set(key: str, value: Any) -> None:
    try:
        _ctx.state.set(key, value)
    except Exception as error:
        logger.warning("boreal: couldn't save %s: %s", key, error)


def _key() -> Optional[str]:
    """The agent key: BOREAL_API_KEY if the owner set one, else the one boreal_check_link collected."""
    try:
        from agent.secret_scope import get_secret

        configured = (get_secret("BOREAL_API_KEY") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    keys = _state_get("keys", {}) or {}
    return keys.get(_base_url())


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _spent_today_cents() -> int:
    spend = _state_get("spend", {}) or {}
    return int(spend.get(f"{_base_url()}|{_today()}", 0))


def _record_spend(cents: int) -> None:
    spend = _state_get("spend", {}) or {}
    slot = f"{_base_url()}|{_today()}"
    # Keep only this week's days so state stays small.
    spend = {k: v for k, v in spend.items() if k.split("|")[-1] >= (_dt.date.today() - _dt.timedelta(days=7)).isoformat()}
    spend[slot] = int(spend.get(slot, 0)) + cents
    _state_set("spend", spend)


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ---------- tool plumbing ----------

def _out(value: Any) -> str:
    return json.dumps(value, default=str)


def _collect_pending_key() -> bool:
    """If a link is waiting for this site, ask boreal.work once whether the owner approved it. True if a key arrived."""
    link = _state_get("link") or {}
    if not link.get("pollToken") or link.get("site") != _base_url():
        return False
    reply = client.call(_base_url(), "POST", "/api/v1/link/poll", body={"pollToken": link["pollToken"]}, timeout=8)
    body = reply.body if isinstance(reply.body, dict) else {}
    if body.get("status") == "approved" and body.get("key"):
        keys = _state_get("keys", {}) or {}
        keys[_base_url()] = body["key"]
        _state_set("keys", keys)
        _state_set("link", None)
        return True
    if body.get("status") in {"expired", "denied", "collected"}:
        _state_set("link", None)
    return False


def _need_key() -> Optional[str]:
    if _key() or _collect_pending_key():
        return None
    return _out({"error": "not_connected", "message": "No boreal.work key yet. Call boreal_connect with your name, send your owner the approveUrl, then boreal_check_link."})


def _api(method: str, path: str, **kwargs: Any) -> client.Reply:
    return client.call(_base_url(), method, path, key=_key(), **kwargs)


def _task_total_cents(task: dict) -> Optional[int]:
    if isinstance(task.get("total"), int):
        return task["total"]
    parts = [task.get("budget"), task.get("platformFee"), task.get("reviewFee")]
    return sum(p for p in parts if isinstance(p, int)) if isinstance(parts[0], int) else None


# ---------- tools ----------

def boreal_connect(args: dict, **_: Any) -> str:
    name = str(args.get("agent_name") or "").strip()
    if not name:
        return _out({"error": "invalid", "message": "agent_name is required"})
    reply = client.call(_base_url(), "POST", "/api/v1/link", body={"agentName": name, "agentDescription": args.get("description") or None})
    if reply.ok and isinstance(reply.body, dict) and reply.body.get("pollToken"):
        _state_set("link", {"site": _base_url(), "pollToken": reply.body["pollToken"], "approveUrl": reply.body.get("approveUrl"), "expiresAt": reply.body.get("expiresAt")})
        body = {k: v for k, v in reply.body.items() if k != "pollToken"}
        return _out({**body, "next": "Send your owner the approveUrl. They log in, set your limits and approve. Then call boreal_check_link."})
    return _out(reply.as_tool_result())


def boreal_check_link(args: dict, **_: Any) -> str:
    link = _state_get("link") or {}
    if not link.get("pollToken") or link.get("site") != _base_url():
        return _out({"error": "no_link", "message": "Call boreal_connect first."})
    reply = client.call(_base_url(), "POST", "/api/v1/link/poll", body={"pollToken": link["pollToken"]})
    body = reply.body if isinstance(reply.body, dict) else {}
    if body.get("status") == "approved" and body.get("key"):
        keys = _state_get("keys", {}) or {}
        keys[_base_url()] = body["key"]
        _state_set("keys", keys)
        _state_set("link", None)
        return _out({"status": "approved", "message": "Connected. Your key is saved for this plugin; call boreal_account."})
    return _out({k: v for k, v in body.items() if k != "key"} or reply.as_tool_result())


def boreal_account(args: dict, **_: Any) -> str:
    if not _key() and not _collect_pending_key():
        return _out({"connected": False, "site": _base_url(), "next": "Call boreal_connect to ask your owner for a key."})
    reply = _api("GET", "/api/v1/me")
    result = reply.as_tool_result()
    result["spendGuard"] = {
        "maxTask": _usd(int(float(_setting("max_task_usd", 10)) * 100)),
        "maxDaily": _usd(int(float(_setting("max_daily_usd", 50)) * 100)),
        "approveOver": _usd(int(float(_setting("approve_over_usd", 5)) * 100)),
        "spentToday": _usd(_spent_today_cents()),
    }
    result["site"] = _base_url()
    return _out(result)


def boreal_quote(args: dict, **_: Any) -> str:
    if (missing := _need_key()):
        return missing
    reply = _api("GET", "/api/v1/quote", query={"budget": args.get("budget_cents"), "reviewer": args.get("reviewer")})
    return _out(reply.as_tool_result())


def boreal_post_task(args: dict, **_: Any) -> str:
    if (missing := _need_key()):
        return missing
    hours = float(args.get("deadline_hours") or 24)
    deadline = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    body = {
        "description": args.get("description"),
        "acceptanceCriteria": args.get("acceptance_criteria") or [],
        "resultFormat": args.get("result_format"),
        "budget": args.get("budget_cents"),
        "deadline": deadline,
        "reviewer": args.get("reviewer") or "boreal",
    }
    return _out(_api("POST", "/api/v1/tasks", body=body).as_tool_result())


def boreal_fund_task(args: dict, **_: Any) -> str:
    if (missing := _need_key()):
        return missing
    task_id = str(args.get("task_id") or "")
    before = _api("GET", f"/api/v1/tasks/{task_id}")
    body = {"orderId": args["order_id"]} if args.get("order_id") else None
    reply = _api("POST", f"/api/v1/tasks/{task_id}/fund", body=body)
    result = reply.as_tool_result()
    status = (reply.body or {}).get("status") if isinstance(reply.body, dict) else None
    if reply.ok and status and status != "awaiting_funding":
        total = _task_total_cents(before.body if isinstance(before.body, dict) else {})
        if total:
            _record_spend(total)
    if reply.status == 402:
        result["next"] = "Send your owner the approveUrl in the payment requirement; once they approve, call boreal_fund_task again with order_id."
    return _out(result)


def boreal_list_tasks(args: dict, **_: Any) -> str:
    if (missing := _need_key()):
        return missing
    return _out(_api("GET", "/api/v1/tasks", query={"status": args.get("status")}).as_tool_result())


def _by_task(method: str, suffix: str = "", body: Any = None):
    def handler(args: dict, **_: Any) -> str:
        if (missing := _need_key()):
            return missing
        return _out(_api(method, f"/api/v1/tasks/{args.get('task_id')}{suffix}", body=body).as_tool_result())
    return handler


def boreal_review(args: dict, **_: Any) -> str:
    if (missing := _need_key()):
        return missing
    body = {"outcome": args.get("outcome"), "ruleIds": args.get("rule_ids") or [], "reasoning": args.get("reasoning") or ""}
    return _out(_api("POST", f"/api/v1/tasks/{args.get('task_id')}/review", body=body).as_tool_result())


HANDLERS = {
    "boreal_connect": boreal_connect,
    "boreal_check_link": boreal_check_link,
    "boreal_account": boreal_account,
    "boreal_quote": boreal_quote,
    "boreal_post_task": boreal_post_task,
    "boreal_fund_task": boreal_fund_task,
    "boreal_list_tasks": boreal_list_tasks,
    "boreal_get_task": _by_task("GET"),
    "boreal_get_submission": _by_task("GET", "/submission"),
    "boreal_review": boreal_review,
    "boreal_accept": _by_task("POST", "/accept", body={}),
    "boreal_cancel": _by_task("POST", "/cancel", body={}),
}


# ---------- the spend guard (pre_tool_call) ----------

# A terminal command that moves USDC or NEAR: boreal.work's own payment script, or a raw transfer.
_PAYMENT_COMMAND = re.compile(r"near-pay\.mjs\s+pay\b|\bft_transfer\b|near\s+(tokens|send)\b", re.IGNORECASE)
_BOREAL_MEMO = re.compile(r"boreal:[0-9a-f-]{8,}", re.IGNORECASE)


def guard(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
    args = args if isinstance(args, dict) else {}
    if tool_name == "boreal_fund_task":
        return _guard_funding(args)
    if tool_name in {"terminal", "shell", "execute_code"}:
        command = str(args.get("command") or args.get("code") or "")
        if _PAYMENT_COMMAND.search(command) and not _BOREAL_MEMO.search(command):
            return {
                "action": "block",
                "message": "boreal spend guard: this command moves money but isn't paying a boreal.work task (no boreal:<taskId> memo). "
                "Pay tasks with boreal_fund_task, or ask your owner first.",
            }
    return None


def _guard_funding(args: dict) -> Optional[dict]:
    task_id = str(args.get("task_id") or "")
    if not task_id or not _key():
        return None  # the tool itself explains what's missing
    reply = _api("GET", f"/api/v1/tasks/{task_id}", timeout=8)
    task = reply.body if reply.ok and isinstance(reply.body, dict) else None
    if task is None:
        return {"action": "block", "message": f"boreal spend guard: couldn't read task {task_id} to check its cost, so it wasn't funded. Try again."}
    total = _task_total_cents(task)
    if total is None:
        return {"action": "approve", "message": f"boreal spend guard: couldn't work out the cost of task {task_id}. Approve funding it?"}
    max_task = int(float(_setting("max_task_usd", 10)) * 100)
    max_daily = int(float(_setting("max_daily_usd", 50)) * 100)
    approve_over = int(float(_setting("approve_over_usd", 5)) * 100)
    spent = _spent_today_cents()
    if total > max_task:
        return {"action": "block", "message": f"boreal spend guard: this task costs {_usd(total)}, over your {_usd(max_task)} per-task limit. Raise max_task_usd in the plugin settings, or post a smaller task."}
    if spent + total > max_daily:
        return {"action": "block", "message": f"boreal spend guard: {_usd(total)} would take today's boreal.work spend to {_usd(spent + total)}, over your {_usd(max_daily)} daily limit."}
    if total > approve_over and not args.get("order_id"):
        what = str(task.get("description") or "")[:140]
        return {"action": "approve", "message": f"Fund this boreal.work task for {_usd(total)} on {_base_url()}? \"{what}\" (held until the work passes; {_usd(spent)} spent today)."}
    return None


# ---------- the verdict digest (pre_llm_call) ----------

def digest(is_first_turn: bool = False, **_: Any) -> Optional[str]:
    if not is_first_turn or not _setting("verdict_digest", True) or not _key():
        return None
    reply = _api("GET", "/api/v1/tasks", timeout=5)
    tasks = reply.body if isinstance(reply.body, list) else (reply.body or {}).get("tasks") if isinstance(reply.body, dict) else None
    if not reply.ok or not isinstance(tasks, list):
        return None
    seen = _state_get("seen", {}) or {}
    news = []
    for task in tasks[:50]:
        tid, status = task.get("id"), task.get("status")
        if not tid or seen.get(tid) == status:
            continue
        if status in FINISHED or status in NEEDS_YOU:
            label = str(task.get("description") or "")[:80]
            news.append(f"- {status}: {label} (task {tid})")
        seen[tid] = status
    _state_set("seen", dict(list(seen.items())[-200:]))
    if not news:
        return None
    return "boreal.work: tasks of yours changed since you last looked. Tell the user what came back, and for submitted tasks you review yourself, use boreal_get_submission and boreal_review.\n" + "\n".join(news[:10])


# ---------- /boreal ----------

def status_command(raw_args: str = "", **_: Any) -> str:
    verb = (raw_args or "").strip().lower()
    if verb == "connect":
        result = json.loads(boreal_connect({"agent_name": "Hermes", "description": "Hires verified people for real-world steps"}))
        if result.get("approveUrl"):
            return (f"boreal.work ({_base_url()}): open {result['approveUrl']} to approve Hermes (sign in, set its limits, approve). "
                    "Hermes picks the key up by itself once you have (or run /borealwork check). The link expires in 15 minutes.")
        return f"boreal.work ({_base_url()}): couldn't start the link: {result.get('message') or result.get('error') or result}"
    if verb == "check":
        result = json.loads(boreal_check_link({}))
        if result.get("status") == "approved":
            return f"boreal.work ({_base_url()}): connected. Hermes can now post and fund tasks within your limits."
        return f"boreal.work ({_base_url()}): {result.get('status') or result.get('message') or result.get('error')}. Approve the link first, then run /borealwork check again."
    if not _key() and not _collect_pending_key():
        link = _state_get("link") or {}
        if link.get("site") == _base_url() and link.get("approveUrl"):
            return (f"boreal.work ({_base_url()}): waiting for your approval. Open {link['approveUrl']}, sign in, set Hermes's limits "
                    f"and approve. Hermes picks the key up by itself once you have. Links expire after 15 minutes: then run /borealwork connect again.")
        return f"boreal.work ({_base_url()}): not connected. Run /borealwork connect (or ask Hermes to connect to boreal.work)."
    me = _api("GET", "/api/v1/me", timeout=8)
    tasks = _api("GET", "/api/v1/tasks", timeout=8)
    lines = [f"boreal.work ({_base_url()})"]
    if isinstance(me.body, dict) and me.ok:
        lines.append(f"Agent: {me.body.get('name') or me.body.get('agentName') or 'connected'}; network: {me.body.get('network', '?')}")
    lines.append(f"Spent today (this plugin): {_usd(_spent_today_cents())} of {_usd(int(float(_setting('max_daily_usd', 50)) * 100))}")
    listed = tasks.body if isinstance(tasks.body, list) else (tasks.body or {}).get("tasks", []) if isinstance(tasks.body, dict) else []
    open_ = [t for t in listed if t.get("status") not in FINISHED]
    lines.append(f"Open tasks: {len(open_)}")
    for t in open_[:5]:
        lines.append(f"  {t.get('status')}: {str(t.get('description') or '')[:70]}")
    return "\n".join(lines)


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    for schema in schemas.ALL:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=HANDLERS[schema["name"]], emoji="🌲")
    ctx.register_hook("pre_tool_call", guard)
    ctx.register_hook("pre_llm_call", digest)
    ctx.register_command("borealwork", handler=status_command, description="boreal.work: status; 'connect' to link Hermes, 'check' once approved", args_hint="[connect|check]")
