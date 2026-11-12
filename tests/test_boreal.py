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
        # boreal.work's own count of what this agent funded in the last day; nothing, unless a test says otherwise.
        routes.setdefault(("GET", "/api/v1/me"), (200, {"network": "mainnet", "spentLast24h": 0}))
        client.call = Fake(routes)
        return client.call

    def run_tool(self, name, args):
        """A tool call as Hermes makes it: the pre_tool_call hook first, the tool only if it isn't blocked."""
        verdict = self.ctx.hooks["pre_tool_call"](tool_name=name, args=args)
        if verdict and verdict.get("action") == "block":
            return verdict, None
        return verdict, json.loads(self.ctx.tools[name](args))

    def money_posts(self, fake):
        return [c[2] for c in fake.calls if c[1] == "POST" and c[2].endswith(("/fund", "/accept", "/review"))]

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
        self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
        self.assertEqual(boreal._spent_today_cents(), 625)

    def test_a_402_is_passed_on_with_the_next_step(self):
        self.fake({
            ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK),
            ("POST", f"/api/v1/tasks/{TASK['id']}/fund"): (402, {"accepts": [{"network": "paypal", "extra": {"approveUrl": "https://paypal.example/approve"}}]}),
        })
        result = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})[1]
        self.assertEqual(result["status"], 402)
        self.assertIn("order_id", result["next"])
        self.assertEqual(boreal._spent_today_cents(), 0)

    # ---- "Always" covers one call, never the tool ----

    def test_each_money_call_has_its_own_approval_key(self):
        other = "7b3c1f2e-0000-4000-8000-000000000002"
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK), ("GET", f"/api/v1/tasks/{other}"): (200, {**TASK, "id": other})})
        hook = self.ctx.hooks["pre_tool_call"]
        calls = [
            ("boreal_fund_task", {"task_id": TASK["id"]}),
            ("boreal_fund_task", {"task_id": other}),
            ("boreal_fund_task", {"task_id": TASK["id"], "order_id": "ORDER-1"}),
            ("boreal_accept", {"task_id": TASK["id"]}),
            ("boreal_review", {"task_id": TASK["id"], "outcome": "pass", "reasoning": "ok"}),
            ("boreal_review", {"task_id": TASK["id"], "outcome": "fail", "reasoning": "no"}),
        ]
        keys = []
        for tool, args in calls:
            verdict = hook(tool_name=tool, args=args)
            self.assertEqual(verdict["action"], "approve", tool)
            # Hermes keys Session/Always answers on the bare tool name when a plugin gives no rule_key.
            self.assertTrue(verdict["rule_key"].startswith(f"borealwork:{tool}:"), verdict["rule_key"])
            self.assertNotEqual(verdict["rule_key"], tool)
            keys.append(verdict["rule_key"])
        self.assertEqual(len(set(keys)), len(calls))
        # The same call asks under the same key, and another site's task under another.
        self.assertEqual(hook(tool_name="boreal_accept", args={"task_id": TASK["id"]})["rule_key"], keys[3])
        self.ctx.settings["base_url"] = "https://testnet.boreal.work"
        self.ctx.state.set("keys", {"https://testnet.boreal.work": "bk_test"})
        self.assertNotEqual(hook(tool_name="boreal_accept", args={"task_id": TASK["id"]})["rule_key"], keys[3])

    # ---- the daily cap counts what boreal.work says was spent, not only what this plugin remembers ----

    def test_the_daily_cap_survives_lost_plugin_state(self):
        # A new profile: nothing recorded locally, but the agent funded $48 in the last day.
        fake = self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK), ("GET", "/api/v1/me"): (200, {"spentLast24h": 4800})})
        self.assertEqual(boreal._spent_today_cents(), 0)
        verdict, result = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")
        self.assertIn("$54.25", verdict["message"])
        self.assertIsNone(result)
        self.assertEqual(self.money_posts(fake), [])

    def test_the_daily_cap_uses_the_larger_of_the_two_counts(self):
        boreal._record_spend(3000)
        self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK), ("GET", "/api/v1/me"): (200, {"spentLast24h": 1000})})
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "approve")
        self.assertIn("$30.00 spent", verdict["message"])

    def test_funding_is_blocked_when_the_spend_or_the_cost_cannot_be_read(self):
        for me in ((500, {"error": "internal"}), (200, {"spentLast24h": "lots"}), (200, {"spentLast24h": -5}), (200, {})):
            fake = self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK), ("GET", "/api/v1/me"): me})
            verdict, result = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
            self.assertEqual(verdict["action"], "block", me)
            self.assertEqual(self.money_posts(fake), [], me)
        # A task with no readable cost can't be held to the limits either.
        fake = self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {"id": TASK["id"], "description": "x"})})
        verdict, _ = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")
        self.assertEqual(self.money_posts(fake), [])

    # ---- no money call reaches boreal.work unless the guard has cleared it ----

    def pending_link(self):
        """The owner has approved a link, and its key hasn't been collected yet."""
        self.ctx.state.set("keys", {})
        self.ctx.state.set("link", {"site": "https://boreal.work", "pollToken": "p", "approveUrl": "https://boreal.work/link/X"})

    def test_funding_still_asks_when_the_link_was_approved_but_not_yet_collected(self):
        self.pending_link()
        fake = self.fake({
            ("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_relinked"}),
            ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK),
            ("POST", f"/api/v1/tasks/{TASK['id']}/fund"): (200, {**TASK, "status": "posted"}),
        })
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "approve")
        self.assertIn("$6.25", verdict["message"])
        self.assertEqual(boreal._key(), "bk_relinked")
        self.assertEqual(self.money_posts(fake), [])  # the guard only read

    def test_the_caps_apply_to_the_first_funding_after_a_relink(self):
        self.pending_link()
        fake = self.fake({
            ("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_relinked"}),
            ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "budget": 5000, "platformFee": 550, "reviewFee": 250}),
        })
        verdict, result = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")
        self.assertIsNone(result)
        self.assertEqual(self.money_posts(fake), [])

    def test_accept_and_review_still_ask_when_the_link_was_approved_but_not_yet_collected(self):
        for tool, args in (("boreal_accept", {"task_id": TASK["id"]}), ("boreal_review", {"task_id": TASK["id"], "outcome": "pass", "reasoning": "ok"})):
            self.pending_link()
            fake = self.fake({("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_relinked"}), ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "status": "passed"})})
            verdict = self.ctx.hooks["pre_tool_call"](tool_name=tool, args=args)
            self.assertEqual(verdict["action"], "approve", tool)
            self.assertEqual(self.money_posts(fake), [], tool)

    def test_a_money_tool_never_collects_a_key_and_goes_on(self):
        """Called without the guard (as if the hook hadn't fired), each money tool sends nothing: not even the poll."""
        for tool, args in (
            ("boreal_fund_task", {"task_id": TASK["id"]}),
            ("boreal_fund_task", {"task_id": TASK["id"], "order_id": "ORDER-1"}),
            ("boreal_accept", {"task_id": TASK["id"]}),
            ("boreal_review", {"task_id": TASK["id"], "outcome": "fail", "rule_ids": ["R4.1"], "reasoning": "x"}),
        ):
            self.pending_link()
            fake = self.fake({("POST", "/api/v1/link/poll"): (200, {"status": "approved", "key": "bk_relinked"})})
            result = json.loads(self.ctx.tools[tool](args))
            self.assertEqual(result["error"], "not_connected", tool)
            self.assertEqual(fake.calls, [], tool)

    def test_a_money_tool_refuses_a_call_the_guard_has_not_cleared(self):
        for tool, args, path in (
            ("boreal_fund_task", {"task_id": TASK["id"]}, "/fund"),
            ("boreal_accept", {"task_id": TASK["id"]}, "/accept"),
            ("boreal_review", {"task_id": TASK["id"], "outcome": "pass", "reasoning": "ok"}, "/review"),
        ):
            fake = self.fake({
                ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, {**TASK, "status": "passed" if tool != "boreal_fund_task" else "awaiting_funding"}),
                ("POST", f"/api/v1/tasks/{TASK['id']}{path}"): (200, {**TASK, "status": "posted"}),
            })
            # Straight to the tool, with a key held: refused, nothing sent.
            self.assertEqual(json.loads(self.ctx.tools[tool](args))["error"], "not_approved", tool)
            self.assertEqual(fake.calls, [], tool)
            # Through the guard: it runs, once.
            verdict, result = self.run_tool(tool, args)
            self.assertEqual(verdict["action"], "approve", tool)
            self.assertEqual(self.money_posts(fake), [f"/api/v1/tasks/{TASK['id']}{path}"], tool)
            # The approval was for that one call: a second needs its own.
            self.assertEqual(json.loads(self.ctx.tools[tool](args))["error"], "not_approved", tool)
            self.assertEqual(len(self.money_posts(fake)), 1, tool)

    def test_an_approval_clears_only_the_call_it_was_given_for(self):
        other = "7b3c1f2e-0000-4000-8000-000000000002"
        fake = self.fake({("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK)})
        self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        for args in ({"task_id": other}, {"task_id": TASK["id"], "order_id": "ORDER-9"}):
            self.assertEqual(json.loads(self.ctx.tools["boreal_fund_task"](args))["error"], "not_approved")
        self.ctx.hooks["pre_tool_call"](tool_name="boreal_review", args={"task_id": TASK["id"], "outcome": "fail", "reasoning": "x"})
        self.assertEqual(json.loads(self.ctx.tools["boreal_review"]({"task_id": TASK["id"], "outcome": "pass", "reasoning": "x"}))["error"], "not_approved")
        self.assertEqual(json.loads(self.ctx.tools["boreal_accept"]({"task_id": TASK["id"]}))["error"], "not_approved")
        self.assertEqual(self.money_posts(fake), [])

    def test_a_blocked_or_unreadable_funding_is_not_cleared(self):
        fake = self.fake({})  # the task can't be read: the guard blocks
        verdict = self.ctx.hooks["pre_tool_call"](tool_name="boreal_fund_task", args={"task_id": TASK["id"]})
        self.assertEqual(verdict["action"], "block")
        self.assertEqual(json.loads(self.ctx.tools["boreal_fund_task"]({"task_id": TASK["id"]}))["error"], "not_approved")
        self.assertEqual(self.money_posts(fake), [])

    def test_with_no_key_and_no_link_money_tools_say_to_connect_and_send_nothing(self):
        self.ctx.state.set("keys", {})
        fake = self.fake({})
        verdict, result = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
        self.assertIsNone(verdict)
        self.assertEqual(result["error"], "not_connected")
        self.assertEqual(fake.calls, [])

    def test_an_owner_approval_on_the_site_is_passed_on_with_the_next_step(self):
        self.fake({
            ("GET", f"/api/v1/tasks/{TASK['id']}"): (200, TASK),
            ("POST", f"/api/v1/tasks/{TASK['id']}/fund"): (202, {"approvalRequired": {"id": "a1", "status": "pending", "approveUrl": "https://boreal.work/approve/a1", "total": 625}}),
        })
        _, result = self.run_tool("boreal_fund_task", {"task_id": TASK["id"]})
        self.assertIn("https://boreal.work/approve/a1", result["next"])
        self.assertEqual(boreal._spent_today_cents(), 0)  # nothing is held yet

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
        self.fake({("POST", "/api/v1/link/poll"): (200, {"status": "pending"})})
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


class ClientTest(unittest.TestCase):
    """The HTTP client itself, against servers on this machine."""

    def serve(self, handler):
        import http.server
        import threading

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_key_is_never_sent_over_plain_http_to_another_host(self):
        for site in ("http://boreal.work", "http://example.com", "ftp://boreal.work", "boreal.work", "https://user:pw@boreal.work", ""):
            reply = client.call(site, "GET", "/api/v1/me", key="bk_secret")
            self.assertEqual(reply.status, 0, site)
            self.assertEqual(reply.body["error"], "insecure_site", site)
        self.assertIsNone(client.site_refusal("https://boreal.work"))
        self.assertIsNone(client.site_refusal("https://testnet.boreal.work"))
        self.assertIsNone(client.site_refusal("http://localhost:3000"))

    def test_a_redirect_is_never_followed_so_the_key_goes_nowhere_else(self):
        import http.server

        seen = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        _, elsewhere = self.serve(Elsewhere)

        class Redirects(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"{elsewhere}/steal")
                self.end_headers()

            def log_message(self, *args):
                pass

        _, site = self.serve(Redirects)
        reply = client.call(site, "GET", "/api/v1/me", key="bk_secret")
        self.assertEqual(reply.status, 302)
        self.assertEqual(reply.body["error"], "redirected")
        self.assertEqual(seen, [])

    def test_a_402_still_carries_its_payment_requirement(self):
        import http.server

        class Pay(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(402)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepts": [{"network": "paypal"}]}')

            def log_message(self, *args):
                pass

        _, site = self.serve(Pay)
        reply = client.call(site, "POST", "/api/v1/tasks/x/fund", key="bk_secret", body={})
        self.assertEqual(reply.status, 402)
        self.assertEqual(reply.body["accepts"][0]["network"], "paypal")


if __name__ == "__main__":
    unittest.main()
