"""A small boreal.work REST client: standard library only, JSON in and out, never raises."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

USER_AGENT = "hermes-boreal/0.2"
TIMEOUT_SECONDS = 20


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def site_refusal(base_url: str) -> Optional[str]:
    """Why this site can't be called, or None. The key is only ever sent over HTTPS; plain HTTP
    is allowed for a boreal.work running on this machine (local development), nowhere else."""
    try:
        parts = urllib.parse.urlsplit(base_url)
    except ValueError:
        return f"base_url isn't a URL: {base_url}"
    if parts.username or parts.password:
        return "base_url must not carry a username or password"
    if parts.scheme == "https" and parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return None
    return f"base_url must be an https:// site (got {base_url}); the agent key is never sent over plain HTTP"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is returned as it is, never followed: the Authorization header must not travel
    to wherever a reply points, and requests go to the configured site only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


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
    if (refusal := site_refusal(base_url)):
        return Reply(0, {"error": "insecure_site", "message": refusal})
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
        with _opener.open(request, timeout=timeout) as response:
            return Reply(response.status, _decode(response.read()))
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            error.close()
            return Reply(error.code, {"error": "redirected", "message": "boreal.work answered with a redirect, which this plugin never follows. Check base_url."})
        # 402 is not a failure here: it carries the payment requirement for fund_task.
        try:
            return Reply(error.code, _decode(error.read()))
        finally:
            error.close()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return Reply(0, {"error": "unreachable", "message": f"boreal.work didn't answer: {error}"})


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"text": text[:2000]}
