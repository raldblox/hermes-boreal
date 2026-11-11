"""Tool schemas: what Hermes reads to decide when to call each boreal.work tool."""


def _tool(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
    }


TASK_ID = {"task_id": {"type": "string", "description": "The boreal.work task id."}}

CONNECT = _tool(
    "boreal_connect",
    "Ask your owner for a boreal.work key. Use once, before anything else, when boreal_account says you have no key. "
    "Returns an approveUrl: send it to your owner (they log in, set your spending limits and approve), then call "
    "boreal_check_link until it says approved.",
    {
        "agent_name": {"type": "string", "description": "Your name as your owner knows you, e.g. Hermes."},
        "description": {"type": "string", "description": "One line on what you'll use boreal.work for."},
    },
    ["agent_name"],
)

CHECK_LINK = _tool(
    "boreal_check_link",
    "Check whether your owner approved your boreal.work key. Once approved, the key is kept for you and every boreal_ tool "
    "uses it. Call every few seconds after boreal_connect, at most for 15 minutes.",
)

ACCOUNT = _tool(
    "boreal_account",
    "Your boreal.work account: whether you're connected, your limits, what you've spent today, the network (live or "
    "testnet), and how your owner pays. Call it first in any task-related conversation.",
)

QUOTE = _tool(
    "boreal_quote",
    "What a task costs before you post it: the worker's pay plus boreal.work's service fee.",
    {
        "budget_cents": {"type": "integer", "description": "Worker pay in US cents, e.g. 500 for $5."},
        "reviewer": {"type": "string", "enum": ["boreal", "requester"], "description": "boreal (default) or requester (you review it yourself, no review fee)."},
    },
    ["budget_cents"],
)

POST_TASK = _tool(
    "boreal_post_task",
    "Hire a verified person for a step you can't do yourself: something only someone on the spot can see, check or do "
    "(confirm a supplier's warehouse is real, check a shelf price, photograph a site, count shoppers). First find out "
    "everything you can online yourself. Write criteria a stranger can check, one observable fact each. Posting only "
    "quotes the task; boreal_fund_task pays for it.",
    {
        "description": {"type": "string", "description": "What the person should do, in plain words a stranger can follow (20+ characters)."},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "1-10 checks a third party can verify; the result is judged on these only."},
        "result_format": {"type": "string", "enum": ["text", "photo", "file"], "description": "What the worker submits."},
        "budget_cents": {"type": "integer", "description": "Worker pay in US cents. The service fee is added on top."},
        "deadline_hours": {"type": "number", "description": "Hours until an unclaimed task is refunded. Default 24."},
        "reviewer": {"type": "string", "enum": ["boreal", "requester"], "description": "boreal (default): boreal.work's reviewer judges it. requester: you judge it with boreal_review."},
    },
    ["description", "acceptance_criteria", "result_format", "budget_cents"],
)

FUND_TASK = _tool(
    "boreal_fund_task",
    "Pay for a posted task. The money is held, not charged: the worker is paid only if the result passes, and you get it "
    "back if it fails (less the review part of the fee), expires or is cancelled. With your owner's PayPal saved, this "
    "funds at once within your limits; otherwise it returns a PayPal approveUrl for your owner, then call again with "
    "order_id. The spend guard checks your limits first, and your owner approves every funding in Hermes.",
    {**TASK_ID, "order_id": {"type": "string", "description": "Only after your owner approved a PayPal checkout: the order id."}},
    ["task_id"],
)

LIST_TASKS = _tool(
    "boreal_list_tasks",
    "Your boreal.work tasks, newest first, with their status.",
    {"status": {"type": "string", "description": "Optional filter, e.g. open, submitted, final_pass, final_fail."}},
)

GET_TASK = _tool("boreal_get_task", "One task: its status, money, and verdict (pass or fail, the rules cited and why).", TASK_ID, ["task_id"])

GET_SUBMISSION = _tool("boreal_get_submission", "The worker's result for a task: their text and links to their files.", TASK_ID, ["task_id"])

REVIEW = _tool(
    "boreal_review",
    "Judge a result yourself, only for tasks posted with reviewer: requester. Judge against the task's criteria and the "
    "rulebook (https://boreal.work/rules) only. A fail must cite at least one rule, e.g. R4.1, and name the criterion missed. "
    "One verdict per submission; the worker can dispute it once to a human panel.",
    {
        **TASK_ID,
        "outcome": {"type": "string", "enum": ["pass", "fail"]},
        "rule_ids": {"type": "array", "items": {"type": "string"}, "description": "Rules applied, e.g. [\"R4.1\"]."},
        "reasoning": {"type": "string", "description": "Two or three sentences the worker will read."},
    },
    ["task_id", "outcome", "reasoning"],
)

ACCEPT = _tool("boreal_accept", "Accept a passed result now, so the worker is paid at once instead of after the 48-hour dispute window.", TASK_ID, ["task_id"])

CANCEL = _tool("boreal_cancel", "Withdraw a task nobody has claimed yet. Its money is returned in full.", TASK_ID, ["task_id"])

ALL = [CONNECT, CHECK_LINK, ACCOUNT, QUOTE, POST_TASK, FUND_TASK, LIST_TASKS, GET_TASK, GET_SUBMISSION, REVIEW, ACCEPT, CANCEL]
