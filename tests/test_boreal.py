"""Tests for the spend guard, the verdict digest and key handling, with boreal.work faked.

Run from the repo root: python -m unittest discover -s tests
"""

import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes_boreal", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
boreal = importlib.util.module_from_spec(spec)
sys.modules["hermes_boreal"] = boreal
spec.loader.exec_module(boreal)
client = sys.modules["hermes_boreal.client"]


class State:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class Ctx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.state = State()
        self.tools, self.hooks, self.commands = {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **_):
        self.tools[name] = handler

    def register_hook(self, name, callback, **_):
        self.hooks[name] = callback

    def register_command(self, name, handler, **_):
        self.commands[name] = handler


TASK = {"id": "7b3c1f2e-0000-4000-8000-000000000001", "description": "Photograph the shelf price of SPF50 sunscreen", "status": "awaiting_funding", "budget": 500, "platformFee": 100, "reviewFee": 25}


class Fake:
    """Answers boreal.work calls from a table and records them."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, base_url, method, path, **kwargs):
        self.calls.append((base_url, method, path, kwargs))
        status, body = self.routes.get((method, path), (404, {"error": "not_found"}))
        return client.Reply(status, body)


class BorealTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx()
        boreal.register(self.ctx)
        self.ctx.state.set("keys", {"https://boreal.work": "bk_test"})
        self.real_call = client.call

    def tearDown(self):
        client.call = self.real_call

    def fake(self, routes):
        client.call = Fake(routes)
        return client.call

    def test_registers_every_declared_tool_and_hook(self):
        self.assertEqual(set(self.ctx.tools), {s["name"] for s in boreal.schemas.ALL})
        self.assertEqual(set(self.ctx.hooks), {"pre_tool_call", "pre_llm_call"})
        self.assertIn("borealwork", self.ctx.commands)

    def test_defaults_to_live_boreal_work(self):
        fake = self.fake({("GET", "/api/v1/me"): (200, {"network": "mainnet"})})
        self.ctx.tools["boreal_account"]({})
        self.assertEqual(fake.calls[0][0], "https://boreal.work")

    def test_guard_asks_before_funding_even_a_small_task(self):
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "budget": 100, "platformFee": 40, "reviewFee": 5})})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "approve")
        self.assertIn("$1.45", verdict["message"])

    def test_guard_asks_before_funding_with_an_order_id(self):
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK)})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"], "order_id": "ORDER-1"})
        self.assertEqual(verdict["action"], "approve")
        self.assertIn("$6.25", verdict["message"])

    def test_guard_asks_before_accepting_a_pass(self):
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "status": "passed"})})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_accept", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "approve")
        self.assertIn("$5.00", verdict["message"])

    def test_guard_asks_before_a_verdict_even_when_the_task_cannot_be_read(self):
        self.fake({})
        for outcome in ("pass", "fail"):
            verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_review", args={"task_id": TASK["id"], "outcome": outcome, "reasoning": "x"})
            self.assertEqual(verdict["action"], "approve")
            self.assertIn(f"a {outcome}", verdict["message"])

    def test_task_ids_are_quoted_in_paths(self):
        fake = self.fake({})
        self.ctx.tools["boreal_get_task"]({"task_id": "../me?x=1"})
        self.assertEqual(fake.calls[0][2], "/api/v1/tasks/..%2Fme%3Fx%3D1")

    def test_guard_blocks_over_the_per_task_limit(self):
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "budget": 1500})})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")
        self.assertIn("per-task limit", verdict["message"])

    def test_guard_blocks_over_the_daily_limit(self):
        self.ctx.state.set("spend", {f"https://boreal.work|{boreal._today()}": 4800})
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "budget": 200, "platformFee": 80, "reviewFee": 10})})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")
        self.assertIn("daily limit", verdict["message"])

    def test_guard_blocks_when_it_cannot_read_the_cost(self):
        self.fake({})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")

    def test_guard_blocks_a_terminal_payment_that_is_not_a_task(self):
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "node near-pay.mjs pay --to someone.near --amount 900"})
        self.assertEqual(verdict["action"], "block")
        ok = self.ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": f"node near-pay.mjs pay --to escrow --amount 900 --memo boreal:{TASK['id']}"})
        self.assertIsNone(ok)
        self.assertIsNone(self.ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "ls -la"}))

    def test_funding_records_todays_spend(self):
        self.fake({
            ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK),
            ("POST", f"/api/v1/tasks/{TASK['id']}/fund"): (200, {**TASK, "status": "posted"}),
        })
        self.ctx.tools["boreal_fund_task"]({"task_id": TASK["id"]})
        self.assertEqual(boreal._spent_today_cents(), 625)

    def test_a_402_is_passed_on_with_the_next_step(self):
        self.fake({
            ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK),
            ("POST", f"/api/v1/tasks/{TASK['id']}/fund"): (402, {"accepts": [{"network": "paypal", "extra": {"approveUrl": "https://paypal.example/approve"}}]}),
        })
        result = json.loads(self.ctx.tools["boreal_fund_task"]({"task_id": TASK["id"]}))
        self.assertEqual(result["status"], 402)
        self.assertIn("order_id", result["next"])
        self.assertEqual(boreal._spent_today_cents(), 0)

    def test_digest_reports_changes_once(self):
        self.fake({("GET", "/api/v1/tasks"): (200, [{**TASK, "status": "final_fail"}, {**TASK, "id": "x2", "status": "open"}])})
        first = self.ctx.hooks["pre_llm_call"](is_first_turn=True)
        self.assertIn("final_fail", first)
        self.assertNotIn("open:", first)
        self.assertIsNone(self.ctx.hooks["pre_llm_call"](is_first_turn=True))
        self.assertIsNone(self.ctx.hooks["pre_llm_call"](is_first_turn=False))

    def test_connect_then_check_link_saves_the_key_without_showing_it(self):
        self.ctx.state.set("keys", {})
        self.fake({
            ("POST", "/api/v1/link"): (201, {"code": "ABCD-1234", "approveUrl": "https://boreal.work/link/ABCD-1234", "pollToken": "secret-poll"}),
            ("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_new"}),
        })
        connected = json.loads(self.ctx.tools["boreal_connect"]({"agent_name": "Hermes"}))
        self.assertNotIn("pollToken", connected)
        checked = self.ctx.tools["boreal_check_link"]({})
        self.assertNotIn("bk_new", checked)
        self.assertEqual(boreal._key(), "bk_new")

    def test_tools_say_how_to_connect_without_a_key(self):
        self.ctx.state.set("keys", {})
        result = json.loads(self.ctx.tools["boreal_post_task"]({"description": "x" * 30}))
        self.assertEqual(result["error"], "not_connected")


    def test_status_shows_a_pending_link(self):
        self.ctx.state.set("keys", {})
        self.ctx.state.set("link", {"site": "https://boreal.work", "pollToken": "p", "approveUrl": "https://boreal.work/link/ABCD-1234"})
        self.assertIn("waiting for your approval", self.ctx.commands["borealwork"](""))
        self.assertIn("https://boreal.work/link/ABCD-1234", self.ctx.commands["borealwork"](""))


    def test_slash_command_connects_and_checks(self):
        self.ctx.state.set("keys", {})
        self.fake({
            ("POST", "/api/v1/link"): (201, {"code": "ABCD-1234", "approveUrl": "https://boreal.work/link/ABCD-1234", "pollToken": "p"}),
            ("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_new"}),
        })
        self.assertIn("https://boreal.work/link/ABCD-1234", self.ctx.commands["borealwork"]("connect"))
        self.assertIn("connected", self.ctx.commands["borealwork"]("check"))
        self.assertEqual(boreal._key(), "bk_new")


    def test_a_pending_link_is_collected_without_asking(self):
        self.ctx.state.set("keys", {})
        self.ctx.state.set("link", {"site": "https://boreal.work", "pollToken": "p", "approveUrl": "https://boreal.work/link/X"})
        self.fake({("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_auto"}), ("GET", "/api/v1/me"): (200, {"network": "mainnet"}), ("GET", "/api/v1/tasks"): (200, [])})
        self.assertNotIn("waiting", self.ctx.commands["borealwork"](""))
        self.assertEqual(boreal._key(), "bk_auto")


    def test_connect_reuses_a_link_that_is_still_waiting(self):
        self.ctx.state.set("keys", {})
        self.ctx.state.set("link", {"site": "https://boreal.work", "pollToken": "p", "approveUrl": "https://boreal.work/link/FIRST", "expiresAt": "2999-01-01T00:00:00Z"})
        fake = self.fake({("POST", "/api/v1/link/poll"): (200, {"status": "pending"})})
        result = json.loads(self.ctx.tools["boreal_connect"]({"agent_name": "Hermes"}))
        self.assertEqual(result["approveUrl"], "https://boreal.work/link/FIRST")
        self.assertNotIn(("POST", "/api/v1/link"), [(c[1], c[2]) for c in fake.calls])

    def test_connect_makes_a_new_link_once_the_old_one_expired(self):
        self.ctx.state.set("keys", {})
        self.ctx.state.set("link", {"site": "https://boreal.work", "pollToken": "p", "approveUrl": "https://boreal.work/link/OLD", "expiresAt": "2000-01-01T00:00:00Z"})
        self.fake({("POST", "/api/v1/link/poll"): (200, {"status": "expired"}), ("POST", "/api/v1/link"): (201, {"approveUrl": "https://boreal.work/link/NEW", "pollToken": "q"})})
        self.assertEqual(json.loads(self.ctx.tools["boreal_connect"]({"agent_name": "Hermes"}))["approveUrl"], "https://boreal.work/link/NEW")


if __name__ == "__main__":
    unittest.main()
