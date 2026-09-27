"""End-to-end tests with a scripted local model (no network, no GPU)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ultron.config import DEFAULTS, Config  # noqa: E402
from ultron.core.agent import Agent  # noqa: E402
from ultron.core.llm import LLMResponse  # noqa: E402
from ultron.core.memory import Memory  # noqa: E402
from ultron.runtime import build_registry  # noqa: E402
from ultron.security.audit import AuditLog  # noqa: E402
from ultron.security.policy import PolicyEngine  # noqa: E402


class ScriptedLLM:
    """Replays a fixed list of model outputs, like a tiny local model would."""

    def __init__(self, outputs, model="scripted"):
        self.outputs = list(outputs)
        self.model = model
        self.offline_only = True
        self.base_url = "http://127.0.0.1:1"
        self.prompts = []

    def chat(self, messages, **kwargs):
        self.prompts.append([m.content for m in messages])
        text = self.outputs.pop(0) if self.outputs else '{"action":"final","args":{"answer":"done"}}'
        return LLMResponse(text=text, model=self.model, duration=0.001)

    def health(self):
        return {"ok": True, "provider": "scripted", "url": self.base_url, "model": self.model,
                "models": [self.model], "offline": True}


def make_agent(outputs, tmpdir, confirmer=None):
    data = json.loads(json.dumps(DEFAULTS))
    data["agent"]["workspace"] = str(Path(tmpdir) / "ws")
    data["security"]["sandbox_root"] = data["agent"]["workspace"]
    data["security"]["audit_log"] = str(Path(tmpdir) / "audit.jsonl")
    data["memory"]["db_path"] = str(Path(tmpdir) / "memory.db")
    Path(data["agent"]["workspace"]).mkdir(parents=True, exist_ok=True)
    config = Config(data=data)
    audit = AuditLog(config.path_for("security.audit_log", "audit.jsonl"))
    policy = PolicyEngine(config)
    registry = build_registry(config, policy, audit)
    memory = Memory(config.path_for("memory.db_path", "memory.db"))
    agent = Agent(config, ScriptedLLM(outputs), registry, policy, audit, memory,
                  confirmer=confirmer)
    return agent, config, policy


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_multi_step_tool_use(self):
        workspace = Path(self.tmp.name) / "ws"
        outputs = [
            json.dumps({"thought": "write the report", "action": "write_file",
                        "args": {"path": str(workspace / "r.md"), "content": "hello"}}),
            json.dumps({"thought": "read it back", "action": "read_file",
                        "args": {"path": str(workspace / "r.md")}}),
            json.dumps({"thought": "done", "action": "final",
                        "args": {"answer": "Report written."}}),
        ]
        agent, _config, _policy = make_agent(outputs, self.tmp.name)
        result = agent.run("create a report")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "Report written.")
        self.assertEqual([s.action for s in result.steps], ["write_file", "read_file", "final"])
        self.assertTrue((workspace / "r.md").read_text() == "hello")

    def test_unknown_tool_recovers(self):
        outputs = [
            json.dumps({"thought": "typo", "action": "write_flie", "args": {}}),
            json.dumps({"action": "final", "args": {"answer": "recovered"}}),
        ]
        agent, _c, _p = make_agent(outputs, self.tmp.name)
        result = agent.run("do it")
        self.assertEqual(result.answer, "recovered")
        self.assertEqual(result.steps[0].status, "error")

    def test_dangerous_shell_denied(self):
        outputs = [json.dumps({"action": "run_shell", "args": {"command": "rm -rf /"}})]
        agent, _c, _p = make_agent(outputs, self.tmp.name)
        result = agent.run("nuke it")
        self.assertEqual(result.steps[0].status, "denied")
        self.assertIn("blocked", result.answer.lower())

    def test_ask_requires_confirmation_then_resumes(self):
        workspace = Path(self.tmp.name) / "ws"
        write = json.dumps({"action": "write_file",
                            "args": {"path": str(workspace / "ok.txt"), "content": "x"}})
        outputs = [
            write,                                   # first run asks for approval
            write,                                   # resumed run replays the step
            json.dumps({"action": "final", "args": {"answer": "written"}}),
        ]
        agent, _c, policy = make_agent(outputs, self.tmp.name)
        # force a confirmation for write_file
        policy.overrides["write_file"] = "ask"
        agent.confirmer = None
        result = agent.run("write a file")
        self.assertEqual(result.steps[0].status, "needs_confirmation")
        self.assertIsNotNone(result.pending_approval)
        approved = agent.resume(result, True)
        self.assertEqual(approved.steps[0].status, "ok")
        self.assertTrue((workspace / "ok.txt").exists())

    def test_rejected_approval_stops(self):
        outputs = [json.dumps({"action": "write_file", "args": {"path": "/etc/hosts",
                                                              "content": "bad"}})]
        agent, _c, policy = make_agent(outputs, self.tmp.name)
        policy.overrides["write_file"] = "ask"
        result = agent.run("edit hosts")
        denied = agent.resume(result, False)
        self.assertEqual(denied.status, "rejected")
        self.assertNotIn("bad", Path("/etc/hosts").read_text())

    def test_secrets_are_redacted_in_logs(self):
        workspace = Path(self.tmp.name) / "ws"
        outputs = [
            json.dumps({"action": "write_file",
                        "args": {"path": str(workspace / "s.txt"),
                                 "content": "pan ABCDE1234F and password hunter2"}}),
            json.dumps({"action": "final", "args": {"answer": "ok"}}),
        ]
        agent, config, _p = make_agent(outputs, self.tmp.name)
        agent.run("store secrets")
        audit_text = config.path_for("security.audit_log", "audit.jsonl").read_text()
        self.assertNotIn("hunter2", audit_text)
        self.assertIn("redacted", audit_text)

    def test_tasks_are_persisted(self):
        outputs = [json.dumps({"action": "final", "args": {"answer": "persisted"}})]
        agent, _c, _p = make_agent(outputs, self.tmp.name)
        result = agent.run("remember this")
        stored = agent.memory.task(result.task_id)
        self.assertEqual(stored["status"], "ok")
        self.assertEqual(stored["result"], "persisted")
        self.assertTrue(agent.memory.list_tasks())

    def test_events_emitted(self):
        events = []
        outputs = [
            json.dumps({"action": "list_dir", "args": {"path": "."}}),
            json.dumps({"action": "final", "args": {"answer": "listed"}}),
        ]
        agent, _c, _p = make_agent(outputs, self.tmp.name)
        agent.emit = events.append
        agent.run("what is here")
        kinds = {event["type"] for event in events}
        self.assertIn("run_start", kinds)
        self.assertIn("step", kinds)
        self.assertIn("result", kinds)


class TestDashboardRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from ultron.runtime import Runtime

        data = json.loads(json.dumps(DEFAULTS))
        data["agent"]["workspace"] = str(Path(self.tmp.name) / "ws")
        data["security"]["sandbox_root"] = data["agent"]["workspace"]
        data["security"]["audit_log"] = str(Path(self.tmp.name) / "audit.jsonl")
        data["memory"]["db_path"] = str(Path(self.tmp.name) / "memory.db")
        Path(data["agent"]["workspace"]).mkdir(parents=True, exist_ok=True)
        config = Config(data=data)
        self.runtime = Runtime(config)
        from ultron.server.app import Dashboard

        self.dashboard = Dashboard(self.runtime)

    def call(self, method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        return self.dashboard.handle(method, path, {}, raw,
                                     {"content-type": "application/json"})

    def test_status_route(self):
        status, payload, _ = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertGreater(len(payload["tools"]), 10)

    def test_index_is_served(self):
        status, payload, headers = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"ULTRON", payload)
        self.assertIn("text/html", headers["Content-Type"])

    def test_static_traversal_blocked(self):
        status, _payload, _ = self.call("GET", "/static/../config.py")
        self.assertEqual(status, 404)

    def test_policy_update(self):
        status, payload, _ = self.call("POST", "/api/policy", {"overrides": {"run_shell": "deny"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy"]["overrides"]["run_shell"], "deny")
        self.assertEqual(self.runtime.policy.check("run_shell", risk=0.6).verdict, "deny")

    def test_chat_requires_message(self):
        status, payload, _ = self.call("POST", "/api/chat", {})
        self.assertEqual(status, 400)
        self.assertIn("message", payload["error"])

    def test_fact_memory(self):
        self.call("POST", "/api/memory/fact", {"key": "pan", "value": "ABCDE1234F"})
        facts = self.runtime.memory.recall_facts()
        self.assertEqual(facts[0]["key"], "pan")
        # ...but the audit log redacts the value
        self.assertNotIn("ABCDE1234F", self.runtime.audit.path.read_text())

    def test_unknown_route_404(self):
        status, _payload, _ = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_multipart_parser(self):
        from ultron.server.app import parse_multipart

        body = (b"--X\r\n"
                b'Content-Disposition: form-data; name="note"\r\n\r\nhi\r\n'
                b"--X\r\n"
                b'Content-Disposition: form-data; name="audio"; filename="a.wav"\r\n'
                b"Content-Type: audio/wav\r\n\r\nBYTES\r\n"
                b"--X--\r\n")
        fields, files = parse_multipart(body, "multipart/form-data; boundary=X")
        self.assertEqual(fields["note"], "hi")
        self.assertEqual(files["audio"], ("a.wav", b"BYTES"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
