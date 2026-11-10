"""A small boreal.work REST client: standard library only, JSON in and out, never raises."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

USER_AGENT = "hermes-boreal/0.1"
TIMEOUT_SECONDS = 20


class Reply:
    """One HTTP reply: `status` (0 when the request never got an answer) and the decoded body."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def as_tool_result(self) -> dict:
        if isinstance(self.body, dict):
            return {"status": self.status, **self.body}
        return {"status": self.status, "body": self.body}


def call(base_url: str, method: str, path: str, *, key: Optional[str] = None, body: Any = None,
         query: Optional[dict] = None, timeout: float = TIMEOUT_SECONDS) -> Reply:
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Reply(response.status, _decode(response.read()))
    except urllib.error.HTTPError as error:
        # 402 is not a failure here: it carries the payment requirement for fund_task.
        return Reply(error.code, _decode(error.read()))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return Reply(0, {"error": "unreachable", "message": f"boreal.work didn't answer: {error}"})


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"text": text[:2000]}
