"""Offline tests for the gate. No network: GitHub and model calls are monkeypatched.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import os
import tempfile

GATE_PATH = Path(__file__).parent.parent / "gate" / "hermes_github_gate.py"


def load_gate(config):
    """Import a fresh copy of the gate with the given config dict (None = no config file)."""
    if config is None:
        os.environ["HERMES_GATE_CONFIG"] = os.path.join(tempfile.mkdtemp(), "missing.json")
    else:
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(config, f); f.close()
        os.environ["HERMES_GATE_CONFIG"] = f.name
    spec = importlib.util.spec_from_file_location("gate", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = load_gate({"bot_login": "example-bot", "owner_login": "loqimean", "bot_name": "Hermes"})


def comment_event(body, sender="loqimean", is_pr=False, assignees=()):
    issue = {"number": 7, "title": "Tile cache", "assignees": [{"login": a} for a in assignees]}
    if is_pr:
        issue["pull_request"] = {"url": "x"}
    return {"action": "created", "sender": {"login": sender}, "repository": {"full_name": "loqimean/example"},
            "issue": issue, "comment": {"id": 99, "body": body, "html_url": "https://github.com/c/99"}}


class Harness(unittest.TestCase):
    """Runs gate.main() against a payload with every side effect faked and recorded."""

    def setUp(self):
        self.calls = []
        self.engaged = False
        self.instant = {"reply": None, "reaction": None, "followup": False}
        self.route = {"tier": "none", "why": "test"}
        patches = {
            "react": lambda path: self.calls.append(("react", "eyes", path)) or True,
            "react_with": lambda path, content: self.calls.append(("react", content, path)),
            "gh_post": lambda path, payload: self.calls.append(("post", path, payload.get("body"))) or 201,
            "thread_context": lambda *a, **k: "(thread)",
            "engaged": lambda *a, **k: self.engaged,
            "route": lambda ctx: dict(self.route),
            "instant": lambda ctx: dict(self.instant),
            "log_route": lambda *a, **k: None,
        }
        self._orig = {k: getattr(gate, k) for k in patches}
        for k, v in patches.items():
            setattr(gate, k, v)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(gate, k, v)

    def run_gate(self, payload):
        out, err = io.StringIO(), io.StringIO()
        sys_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    gate.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = sys_stdin
        text = out.getvalue().strip()
        return None if text == "[SILENT]" else json.loads(text)

    def reactions(self):
        return [c[1] for c in self.calls if c[0] == "react"]

    def posts(self):
        return [c[2] for c in self.calls if c[0] == "post"]


class MentionTests(unittest.TestCase):
    def test_mentions(self):
        self.assertTrue(gate.mentions("@example-bot hi"))
        self.assertTrue(gate.mentions("hey @Example-Bot, look"))
        self.assertTrue(gate.mentions("/hermes do it"))
        self.assertFalse(gate.mentions("mail x@example-bot.com"))
        self.assertFalse(gate.mentions("@example-botx"))
        self.assertFalse(gate.mentions(""))

    def test_addressed_elsewhere(self):
        self.assertTrue(gate.addressed_elsewhere("@bob can you check"))
        self.assertFalse(gate.addressed_elsewhere("@bob @example-bot both of you"))
        self.assertFalse(gate.addressed_elsewhere("no tags here"))
        self.assertFalse(gate.addressed_elsewhere("ping me at a@b.com"))


class AccessGuardTests(unittest.TestCase):
    def test_disclaimers_caught(self):
        for bad in ["I don't have access to the PR to review it.",
                    "I can't see the repository from here.",
                    "I'm an AI teammate without repo access by design.",
                    "You can't give me repo access. Just paste code snippets here.",
                    "Can you share the relevant parts of the diff?",
                    "I do not have any access to that code."]:
            self.assertTrue(gate.disclaims_access(bad), bad)

    def test_normal_replies_pass(self):
        for ok in ["Checking the diff, tests, and merge state now.",
                   "Hey! What do you need on this PR?",
                   "**Merge** keeps history, **rebase** rewrites it.",
                   "Access tokens should go in the secrets store, not the repo."]:
            self.assertFalse(gate.disclaims_access(ok), ok)


class DecideTests(unittest.TestCase):
    def test_matrix(self):
        d = gate.decide
        self.assertEqual(d({"ignore": True}, {"tier": "none"}, posted=False), "ignore")
        self.assertEqual(d({"reaction": "heart"}, {"tier": "none"}, posted=False), "none")
        self.assertEqual(d({"reply": "main"}, {"tier": "none"}, posted=True), "none")
        self.assertEqual(d({"reply": "Checking CI", "followup": True}, {"tier": "none"}, posted=True), "sonnet")
        self.assertEqual(d({}, {"tier": "none"}, posted=False), "sonnet")          # instant failed
        self.assertEqual(d({"ignore": True}, {"tier": "opus"}, posted=False), "opus")  # router wins on work
        self.assertEqual(d({"reply": "x"}, {"tier": "opus"}, posted=True), "opus")


class GateFlowTests(Harness):
    def test_stranger_dropped(self):
        self.assertIsNone(self.run_gate(comment_event("@example-bot hi", sender="rando")))
        self.assertEqual(self.calls, [])

    def test_untagged_in_unwatched_thread_dropped(self):
        self.assertIsNone(self.run_gate(comment_event("random note")))
        self.assertEqual(self.calls, [])

    def test_talking_to_someone_else_dropped(self):
        self.engaged = True
        self.assertIsNone(self.run_gate(comment_event("@bob can you look")))
        self.assertEqual(self.calls, [])

    def test_greeting_answered_instantly(self):
        self.instant = {"reply": "Hey! What do you need?", "followup": False}
        self.assertIsNone(self.run_gate(comment_event("@example-bot hi")))
        self.assertEqual(self.posts(), ["Hey! What do you need?"])
        self.assertEqual(self.reactions(), ["eyes", "rocket"])

    def test_em_dashes_removed_from_instant_reply(self):
        self.instant = {"reply": "Squash merge makes one commit\u2014clean history.", "followup": False}
        self.run_gate(comment_event("@example-bot squash vs rebase?"))
        self.assertEqual(self.posts(), ["Squash merge makes one commit, clean history."])

    def test_thanks_gets_reaction_only(self):
        self.instant = {"reply": None, "reaction": "heart"}
        self.assertIsNone(self.run_gate(comment_event("@example-bot thanks!")))
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.reactions(), ["eyes", "heart"])

    def test_work_escalates_to_agent_with_context(self):
        self.instant = {"reply": "Checking the CI logs now.", "followup": True}
        self.route = {"tier": "opus", "why": "deep debugging"}
        out = self.run_gate(comment_event("@example-bot prod 500s, root cause + fix", is_pr=True))
        self.assertEqual(out["bot"]["tier"], "opus")
        self.assertEqual(out["bot"]["instant_reply"], "Checking the CI logs now.")
        self.assertEqual(out["bot"]["target_type"], "PR")
        self.assertEqual(self.reactions(), ["eyes"])          # rocket comes from the agent when it finishes

    def test_access_disclaimer_never_posted_and_escalates(self):
        self.instant = {"reply": "I don't have access to the PR to review it.", "followup": False}
        self.route = {"tier": "none", "why": "looks simple"}
        out = self.run_gate(comment_event("@example-bot ready to merge?", is_pr=True))
        self.assertEqual(self.posts(), [])
        self.assertEqual(out["bot"]["tier"], "sonnet")
        self.assertEqual(out["bot"]["instant_reply"], "(none posted)")

    def test_router_none_but_promise_forces_followup(self):
        self.instant = {"reply": "Looking at the diff now.", "followup": True}
        self.route = {"tier": "none", "why": "looks simple"}
        out = self.run_gate(comment_event("@example-bot is this diff ok?"))
        self.assertEqual(out["bot"]["tier"], "sonnet")

    def test_untagged_followup_in_watched_thread(self):
        self.engaged = True
        self.instant = {"reply": "main", "followup": False}
        self.assertIsNone(self.run_gate(comment_event("what's git's default branch?")))
        self.assertEqual(self.posts(), ["main"])
        self.assertEqual(self.reactions(), ["eyes", "rocket"])   # eyes only after deciding it's for the bot

    def test_untagged_note_to_self_ignored_without_reaction(self):
        self.engaged = True
        self.instant = {"ignore": True}
        self.assertIsNone(self.run_gate(comment_event("todo for me: rename env var")))
        self.assertEqual(self.calls, [])

    def test_assignment_never_stays_instant(self):
        payload = {"action": "assigned", "sender": {"login": "loqimean"}, "assignee": {"login": "example-bot"},
                   "repository": {"full_name": "loqimean/example-api"},
                   "issue": {"number": 3, "title": "Fix tx import", "body": "", "html_url": "u"}}
        self.route = {"tier": "sonnet", "why": "assignment"}
        out = self.run_gate(payload)
        self.assertEqual(out["bot"]["kind"], "assigned to you")
        self.assertEqual(out["bot"]["tier"], "sonnet")

    def test_assignment_to_someone_else_dropped(self):
        payload = {"action": "assigned", "sender": {"login": "loqimean"}, "assignee": {"login": "loqimean"},
                   "repository": {"full_name": "loqimean/example-api"},
                   "issue": {"number": 3, "title": "t", "body": "", "html_url": "u"}}
        self.assertIsNone(self.run_gate(payload))


class ConfigTests(unittest.TestCase):
    def test_unconfigured_gate_drops_everything(self):
        bare = load_gate(None)
        out = io.StringIO()
        sys_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(comment_event("@example-bot hi")))
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    bare.main()
        finally:
            sys.stdin = sys_stdin
        self.assertEqual(out.getvalue().strip(), "[SILENT]")

    def test_custom_bot_login_and_name(self):
        other = load_gate({"bot_login": "acme-bot", "owner_login": "alice", "bot_name": "Robo"})
        self.assertTrue(other.mentions("@acme-bot ping"))
        self.assertFalse(other.mentions("@example-bot ping"))
        self.assertTrue(other.addressed_elsewhere("@example-bot ping"))
        self.assertIn("Robo", other.INSTANT_PROMPT)
        self.assertNotIn("BOTNAME", other.INSTANT_PROMPT)


class RouteGuardTests(unittest.TestCase):
    def test_assignment_router_none_is_bumped(self):
        orig = gate.llm_json
        gate.llm_json = lambda *a, **k: {"tier": "none", "why": "x", "_model": "m"}
        try:
            r = gate.route({"kind": "assigned to you", "repo": "a/b", "target_type": "issue", "number": 1,
                            "title": "t", "text": ""})
        finally:
            gate.llm_json = orig
        self.assertEqual(r["tier"], "sonnet")

    def test_router_failure_falls_back_to_sonnet(self):
        orig = gate.llm_json

        def boom(*a, **k):
            raise TimeoutError("slow")
        gate.llm_json = boom
        try:
            with redirect_stderr(io.StringIO()):
                r = gate.route({"kind": "mention in comment", "repo": "a/b", "target_type": "issue", "number": 1,
                                "title": "t", "text": "hi"})
        finally:
            gate.llm_json = orig
        self.assertEqual(r["tier"], "sonnet")
        self.assertIn("TimeoutError", r["error"])


if __name__ == "__main__":
    unittest.main()
