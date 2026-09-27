"""Ultron agent runtime.

A local, tool-using ReAct loop with:
  * strict JSON action protocol (robust to fenced output)
  * per-step policy gating with optional human confirmation
  * streaming events for the dashboard (SSE) and voice
  * memory retrieval and durable task persistence
  * secret redaction on everything that flows back to the model
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..security.audit import AuditLog, _is_secret_key
from ..security.policy import DENY, Decision, PolicyEngine, scan_secrets
from ..tools.registry import Tool, ToolRegistry
from .llm import LocalLLM, LLMUnavailable, Message

SYSTEM_PROMPT = """You are Ultron, a private offline assistant that runs entirely on the user's own machine.

Rules:
1. Think in one short sentence, then act.
2. Reply with EXACTLY ONE JSON object and nothing else. No markdown fences, no prose.
3. Use one of these shapes:
   {"thought": "...", "action": "tool_name", "args": {...}}
   {"thought": "...", "action": "final", "args": {"answer": "..."}}
4. Only call tools from the tool list. Arguments must match the schema types.
5. Chain tools: use the previous tool output to build the next call.
6. Financial work: compute and show your working. Never claim a filing happened.
7. Never invent file paths, figures or tool results. If you need a file, list the directory first.
8. Respect refusals: if a tool result says denied or asks for confirmation, stop and explain to the user.

Available tools:
{tools}
"""

PLANNER_PROMPT = """Break the user's goal into the shortest possible list of concrete steps.
Each step must be independently executable by an agent that can call these tools:
{tools}

Reply with EXACTLY one JSON object:
{{"goal_summary": "...", "steps": [{{"id": 1, "title": "...", "detail": "...", "needs_confirmation": false}}]}}

Rules: 3-8 steps, no filler, each step must name the tool it will use, and steps that
write files, send messages, pay money or change system state must set needs_confirmation true.
"""

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
ACTION_KEYS = ("action", "tool", "tool_name", "function")


@dataclass
class Step:
    index: int
    thought: str = ""
    action: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"      # pending | ok | denied | error | needs_confirmation
    result: Any = None
    error: str = ""
    risk: float = 0.0
    duration: float = 0.0
    started: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "thought": self.thought, "action": self.action,
            "args": _redact(self.args), "status": self.status, "result": _clip(self.result),
            "error": self.error, "risk": round(self.risk, 3),
            "duration": round(self.duration, 3),
        }


@dataclass
class RunResult:
    answer: str
    steps: List[Step]
    task_id: str
    duration: float
    status: str = "ok"
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    pending_approval: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "task_id": self.task_id,
            "status": self.status,
            "duration": round(self.duration, 2),
            "model": self.model,
            "usage": self.usage,
            "steps": [step.to_dict() for step in self.steps],
            "pending_approval": self.pending_approval,
        }


Confirmer = Callable[[Decision, Dict[str, Any]], Dict[str, Any]]


class Agent:
    def __init__(
        self,
        config: Any,
        llm: LocalLLM,
        registry: ToolRegistry,
        policy: PolicyEngine,
        audit: AuditLog,
        memory: Any,
        confirmer: Optional[Confirmer] = None,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self.memory = memory
        self.confirmer = confirmer
        self.emit = emit or (lambda event: None)
        self.max_steps = int(config.get("agent.max_steps", 24))
        self.history_limit = int(config.get("agent.history_limit", 200))
        self.autonomous = bool(config.get("agent.autonomous", False))
        self.name = str(config.get("agent.name", "Ultron"))
        self.redact = bool(config.get("security.redact_secrets_in_output", True))

    # -- prompting ---------------------------------------------------------
    def system_prompt(self, extra: str = "") -> str:
        # str.replace, not format: the prompt itself is full of literal braces.
        prompt = SYSTEM_PROMPT.replace("{tools}", self.registry.prompt_block())
        return prompt + (f"\n{extra}" if extra else "")

    def _context(self, session: str, goal: str) -> str:
        parts: List[str] = []
        facts = self.memory.recall_facts(8)
        if facts:
            parts.append("Known facts:\n" + "\n".join(f"- {f['key']}: {f['value']}" for f in facts))
        related = self.memory.retrieve(session, goal, 3)
        if related:
            parts.append("Related earlier context:\n" +
                         "\n".join(f"- {item['text'][:300]}" for item in related))
        recent = self.memory.history(session, 6)
        if recent:
            parts.append("Recent conversation:\n" +
                         "\n".join(f"{m['role']}: {m['content'][:400]}" for m in recent))
        return "\n\n".join(parts)

    # -- planning ----------------------------------------------------------
    def plan(self, goal: str) -> Dict[str, Any]:
        messages = [
            Message("system", PLANNER_PROMPT.replace("{tools}", self.registry.prompt_block())),
            Message("user", goal),
        ]
        response = self.llm.chat(messages, model=getattr(self.llm, "planner_model", "") or None, temperature=0.0)
        parsed = parse_json(response.text)
        if not isinstance(parsed, dict) or "steps" not in parsed:
            return {"ok": False, "error": "planner returned malformed JSON",
                    "raw": response.text[:2000], "fallback_steps": [goal]}
        steps = [s for s in parsed.get("steps", []) if isinstance(s, dict)][:12]
        self.audit.record("agent.plan", detail={"goal": goal[:300], "steps": len(steps)})
        self.emit({"type": "plan", "goal": goal, "steps": steps,
                   "summary": parsed.get("goal_summary", "")})
        return {"ok": True, "summary": parsed.get("goal_summary", ""), "steps": steps}

    # -- execution ---------------------------------------------------------
    def run(self, goal: str, *, session: str = "default", task_id: Optional[str] = None,
            context: str = "", allow_tools: bool = True) -> RunResult:
        task_id = task_id or uuid.uuid4().hex[:12]
        started = time.time()
        steps: List[Step] = []
        self.memory.add_message(session, "user", goal)
        self.emit({"type": "run_start", "task_id": task_id, "goal": goal, "session": session})
        self.memory.upsert_task({"id": task_id, "title": goal[:80], "goal": goal,
                                 "status": "running", "created": started, "session": session,
                                 "steps": []})

        if not allow_tools:
            return self._answer_only(goal, session, task_id, steps, started, context)

        messages: List[Message] = [
            Message("system", self.system_prompt(self._context(session, goal))),
            Message("user", goal + (f"\n\nAdditional context: {context}" if context else "")),
        ]

        answer = ""
        status = "ok"
        pending: Optional[Dict[str, Any]] = None
        usage = {"prompt_tokens": 0, "completion_tokens": 0}

        for index in range(1, self.max_steps + 1):
            try:
                response = self.llm.chat(messages)
            except LLMUnavailable as exc:
                status = "error"
                answer = f"The local model is unavailable: {exc}"
                break
            usage["prompt_tokens"] += response.prompt_tokens
            usage["completion_tokens"] += response.completion_tokens
            decision = parse_json(response.text)
            if decision is None:
                answer = response.text.strip()
                status = "ok"
                break
            action = _first(decision, *ACTION_KEYS) or "final"
            args = decision.get("args") or decision.get("arguments") or {}
            if not isinstance(args, dict):
                args = {"value": args}
            thought = str(decision.get("thought") or decision.get("reasoning") or "")

            if action in ("final", "answer", "done", "stop", "reply"):
                answer = str(args.get("answer") or args.get("text") or decision.get("answer", ""))
                steps.append(Step(index=index, thought=thought, action="final",
                                  args={"answer": answer}, status="ok"))
                self.emit({"type": "step", "task_id": task_id, "step": steps[-1].to_dict()})
                break

            step = Step(index=index, thought=thought, action=action, args=args, status="running")
            steps.append(step)
            self.emit({"type": "step_start", "task_id": task_id,
                       "index": index, "action": action, "args": _redact(args)})

            tool = self.registry.get(action)
            if tool is None:
                step.status = "error"
                step.error = f"unknown tool '{action}'. Available: {', '.join(self.registry.names())}"
                messages.append(Message("assistant", response.text))
                messages.append(Message("user", _step_feedback(step)))
                self.emit({"type": "step", "task_id": task_id, "step": step.to_dict()})
                continue

            verdict = self._gate(tool, args, step)
            if verdict.verdict == "deny":
                step.status = "denied"
                step.risk = verdict.risk
                step.error = verdict.reason
                answer = (f"I stopped: {action} was blocked by policy. {verdict.reason}. "
                          "Change the policy in ultron.config.json if you want to allow it.")
                self.audit.record("agent.denied", status="denied", risk=verdict.risk,
                                  detail={"tool": action, "reason": verdict.reason}, task_id=task_id)
                self.emit({"type": "step", "task_id": task_id, "step": step.to_dict()})
                break
            if verdict.verdict == "ask":
                step.status = "needs_confirmation"
                step.risk = verdict.risk
                pending = {
                    "task_id": task_id, "index": index, "tool": action,
                    "args": _redact(args), "risk": verdict.risk, "band": verdict.band,
                    "reason": verdict.reason, "capability": tool.capability,
                    "description": tool.description,
                }
                answer = (f"{action} needs your approval ({verdict.reason}). "
                          "Approve or reject in the dashboard to continue.")
                self.audit.record("agent.awaiting_approval", risk=verdict.risk,
                                  detail={"tool": action, "reason": verdict.reason}, task_id=task_id)
                self.emit({"type": "approval_required", "task_id": task_id, "pending": pending})
                self.emit({"type": "step", "task_id": task_id, "step": step.to_dict()})
                break

            step.risk = verdict.risk
            try:
                result = self.registry.dispatch(action, args)
                step.status = "ok"
                step.result = result
            except Exception as exc:  # tool failure is data, not a crash
                step.status = "error"
                step.error = f"{type(exc).__name__}: {exc}"
                result = {"ok": False, "error": step.error}
                step.result = result
            step.duration = time.time() - step.started
            self.emit({"type": "step", "task_id": task_id, "step": step.to_dict()})
            messages.append(Message("assistant", response.text))
            messages.append(Message("user", _step_feedback(step)))
        else:
            status = "max_steps"
            answer = (f"I reached the {self.max_steps}-step limit without a final answer. "
                      "Narrow the goal or raise agent.max_steps.")

        result = RunResult(answer=answer, steps=steps, task_id=task_id,
                           duration=time.time() - started, status=status,
                           model=self.llm.model, usage=usage, pending_approval=pending)
        self._persist(session, goal, result)
        return result

    def run_stream(self, goal: str, *, session: str = "default", task_id: Optional[str] = None,
                   context: str = "") -> Any:
        """Yield events as they happen, then a final `result` event."""
        collected: List[Dict[str, Any]] = []
        original = self.emit

        def collector(event: Dict[str, Any]) -> None:
            collected.append(event)
            original(event)

        self.emit = collector
        try:
            result = self.run(goal, session=session, task_id=task_id, context=context)
        finally:
            self.emit = original
        for event in collected:
            yield event
        yield {"type": "result", "result": result.to_dict()}

    def resume(self, run: RunResult, approved: bool) -> RunResult:
        """Continue after the user approved or rejected a pending step."""
        pending = run.pending_approval
        if not pending:
            return run
        task = self.memory.task(run.task_id) or {}
        if not approved:
            self.audit.record("agent.approval_rejected", risk=pending.get("risk", 0),
                              detail={"tool": pending.get("tool")}, task_id=run.task_id)
            return RunResult(
                answer="Understood, I did not run that step.",
                steps=run.steps, task_id=run.task_id, duration=run.duration,
                status="rejected", model=run.model,
            )
        self.policy.grant(Decision(pending["tool"], pending.get("risk", 0), "allow",
                                   "user approved", "user-approved", False), approver="user")
        return self.run(
            f"Continue the task. Run exactly this step now: {json.dumps(pending['args'])} "
            f"using tool `{pending['tool']}`, then give the final answer.",
            session=task.get("session", "default"), task_id=run.task_id,
        )

    # -- helpers -----------------------------------------------------------
    def _answer_only(self, goal: str, session: str, task_id: str,
                     steps: List[Step], started: float, context: str) -> RunResult:
        messages = [
            Message("system", "You are Ultron, a private offline assistant. Be precise and concise."),
            Message("user", goal + (f"\n\nContext: {context}" if context else "")),
        ]
        try:
            response = self.llm.chat(messages)
            answer = response.text
        except LLMUnavailable as exc:
            answer = f"The local model is unavailable: {exc}"
        result = RunResult(answer=answer, steps=steps, task_id=task_id,
                           duration=time.time() - started, model=self.llm.model)
        self._persist(session, goal, result)
        return result

    def _gate(self, tool: Tool, args: Dict[str, Any], step: Step) -> Decision:
        path_arg = None
        for name in tool.confirm_args:
            if name in args and isinstance(args[name], str):
                path_arg = args[name]
                break
        extra = 0.0
        if tool.name in ("run_shell", "run_python") and isinstance(args.get("command"), str):
            if len(args["command"]) > 400 or "&&" in args["command"]:
                extra += 0.05
        if tool.name == "run_shell" and isinstance(args.get("command"), str):
            from ..tools.shell_tools import scan_command

            findings = scan_command(args["command"])
            if findings:
                decision = Decision(tool.name, 0.95, DENY,
                                    f"command matches the safety scanner: {', '.join(findings)}",
                                    "critical", True, None)
                self.audit.record(f"tool.{tool.name}", status="denied", risk=decision.risk,
                                  detail={"verdict": "deny", "reason": decision.reason,
                                          "findings": findings, "args": _redact(args)})
                return decision
        if path_arg and not tool.capability.startswith("fs."):
            path_arg = None
        decision = self.policy.check(
            tool.name, risk=tool.risk, capability=tool.capability,
            path=path_arg, detail={"tool": tool.name, "args": _redact(args)}, extra_risk=extra,
        )
        if decision.verdict == "ask" and self._already_granted(tool.name, path_arg):
            decision.verdict = "allow"
            decision.requires_confirmation = False
            decision.reason = "allowed by an earlier session grant"
        if decision.verdict == "ask" and self.autonomous and decision.risk < self.policy.deny_at:
            decision.verdict = "allow"
            decision.requires_confirmation = False
            decision.reason += " (auto-approved in autonomous mode)"
        if decision.verdict == "ask" and self.confirmer is not None:
            response = self.confirmer(decision, {"tool": tool.name, "args": _redact(args)})
            if isinstance(response, dict):
                if response.get("approve") or response.get("verdict") == "allow":
                    decision.verdict = "allow"
                    decision.requires_confirmation = False
                    self.policy.grant(decision, approver="user")
            elif response is True:
                decision.verdict = "allow"
                decision.requires_confirmation = False
                self.policy.grant(decision, approver="user")
        self.audit.record(f"tool.{tool.name}", status="ok" if decision.allowed else "denied",
                          risk=decision.risk,
                          detail={"verdict": decision.verdict, "reason": decision.reason,
                                  "args": _redact(args)})
        return decision

    def _already_granted(self, action: str, path: Optional[str]) -> bool:
        for grant in self.policy.approvals:
            if grant.get("action") != action:
                continue
            granted_path = grant.get("path")
            if granted_path in (None, path):
                return True
        return False

    def _persist(self, session: str, goal: str, result: RunResult) -> None:
        self.memory.add_message(session, "assistant", result.answer)
        self.memory.index_session(session, [goal, result.answer][:2])
        self.memory.upsert_task({
            "id": result.task_id, "title": goal[:80], "goal": goal, "status": result.status,
            "session": session, "steps": [step.to_dict() for step in result.steps],
            "result": result.answer, "error": "",
        })
        self.emit({"type": "result", "result": result.to_dict()})


# --------------------------------------------------------------------------
# JSON protocol helpers
# --------------------------------------------------------------------------
def _first(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key]:
            return data[key]
    return None


def parse_json(text: str) -> Optional[Any]:
    """Tolerate fences, leading prose and trailing chatter."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"```$", "", candidate.strip())
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    match = JSON_BLOCK.search(candidate)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # last resort: scan objects from the end (models often add prose after)
    for index in range(len(candidate)):
        if candidate[index] != "{":
            continue
        for end in range(len(candidate), index, -1):
            chunk = candidate[index:end]
            if not chunk.endswith("}"):
                continue
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return None


def _step_feedback(step: Step) -> str:
    payload = {
        "tool": step.action,
        "status": step.status,
        "result": _clip(step.result),
        "error": step.error,
    }
    if step.status == "denied":
        payload["instruction"] = "This was denied. Do not retry it; explain the denial to the user."
    elif step.status == "error":
        payload["instruction"] = "Fix the arguments and try a different approach, or explain the failure."
    else:
        payload["instruction"] = "Use this result to decide the next action."
    return "TOOL_RESULT " + json.dumps(payload, default=str)


def _clip(value: Any, limit: int = 6000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...<truncated {len(value) - limit} chars>"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:200]]
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[key] = "***redacted***" if _is_secret_key(key) else _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        found = scan_secrets(value)
        for secret in found:
            if len(secret) > 6:
                value = value.replace(secret, "***redacted***")
        return value
    return value
