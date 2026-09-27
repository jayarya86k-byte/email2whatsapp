"""Ultron command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_config, write_default_config
from .runtime import Runtime
from .security.vault import Vault, get_keyring_backend, mask

BANNER = r"""
  __      __      _                 
 /  |    /  \    | |                
/ / |   / / \   | |_ _   _ _ __ ___ 
/ /  |  / / _ \  | __| | | | '_ ` _ \
\ \_  | / ___ \ | |_| |_| | | | | | |
 \___|/_/   \_\_\__|\__,_|_|_| |_| |_|  offline · private · yours
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ultron", description="Ultron, a local-first super agent")
    parser.add_argument("-c", "--config", help="path to ultron.config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="single task in the terminal")
    run.add_argument("goal", nargs="+", help="what Ultron should do")
    run.add_argument("--session", default="default")
    run.add_argument("--plan", action="store_true", help="show a plan before acting")
    run.add_argument("--no-tools", action="store_true", help="answer only, no tool calls")
    run.add_argument("--yes", action="store_true", help="auto-approve below deny threshold")

    chat = sub.add_parser("chat", help="interactive REPL")
    chat.add_argument("--session", default="default")

    serve = sub.add_parser("serve", help="start the dashboard")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-browser", action="store_true")

    sub.add_parser("status", help="show model, tools, policy and audit health")
    sub.add_parser("doctor", help="run preflight checks")

    tools = sub.add_parser("tools", help="list registered tools")
    tools.add_argument("--json", action="store_true")

    tasks = sub.add_parser("tasks", help="list recent tasks")
    tasks.add_argument("-n", type=int, default=20)

    audit = sub.add_parser("audit", help="inspect the tamper-evident log")
    audit.add_argument("-n", type=int, default=25)
    audit.add_argument("--verify", action="store_true", help="verify the hash chain only")

    init = sub.add_parser("init", help="write a starter config")
    init.add_argument("--path", default="~/.ultron/ultron.config.json")

    vault = sub.add_parser("vault", help="credential vault")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    vault_sub.add_parser("status")
    set_cmd = vault_sub.add_parser("set")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value")
    get_cmd = vault_sub.add_parser("get")
    get_cmd.add_argument("key")
    list_cmd = vault_sub.add_parser("list")
    list_cmd.add_argument("--show", action="store_true", help="print full values")

    challan = sub.add_parser("challan", help="generate a tax challan")
    challan.add_argument("type", help="income_tax | tds | cess | gst | self_assessment")
    challan.add_argument("amount", type=float)
    challan.add_argument("-o", "--out", default="")
    challan.add_argument("--pan", default="")
    challan.add_argument("--name", default="")
    challan.add_argument("--ay", default="", help="assessment year, e.g. 2026-27")

    tax = sub.add_parser("tax", help="compute income tax from a JSON file or - for stdin")
    tax.add_argument("input", help="path to a JSON income description, or '-'")
    tax.add_argument("--ay", default="")
    tax.add_argument("--regime", default="new", choices=["new", "old"])
    tax.add_argument("-o", "--out", default="")

    gst = sub.add_parser("gst", help="compute a GST invoice from a JSON file")
    gst.add_argument("input", help="path to a JSON invoice description")
    gst.add_argument("-o", "--out", default="")

    voice = sub.add_parser("voice", help="transcribe a wav file or speak a line")
    voice.add_argument("action", choices=["stt", "tts", "backends"])
    voice.add_argument("target", nargs="?", default="")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:  # surface the message, keep the traceback for --debug
        print(f"ultron: {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("ULTRON_DEBUG"):
            raise
        return 1


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        target = write_default_config(args.path)
        print(f"wrote {target}")
        return 0

    runtime = Runtime(load_config(getattr(args, "config", None)))

    if args.command == "run":
        return cmd_run(runtime, args)
    if args.command == "chat":
        return cmd_chat(runtime, args)
    if args.command == "serve":
        return cmd_serve(runtime, args)
    if args.command == "status":
        return cmd_status(runtime)
    if args.command == "doctor":
        return cmd_doctor(runtime)
    if args.command == "tools":
        return cmd_tools(runtime, args)
    if args.command == "tasks":
        return cmd_tasks(runtime, args)
    if args.command == "audit":
        return cmd_audit(runtime, args)
    if args.command == "vault":
        return cmd_vault(runtime, args)
    if args.command == "challan":
        return cmd_challan(runtime, args)
    if args.command == "tax":
        return cmd_tax(runtime, args)
    if args.command == "gst":
        return cmd_gst(runtime, args)
    if args.command == "voice":
        return cmd_voice(runtime, args)
    parser.error(f"unknown command {args.command}")
    return 1


def cmd_run(runtime: Runtime, args: argparse.Namespace) -> int:
    goal = " ".join(args.goal)
    if args.yes:
        runtime.agent.autonomous = True
    if args.plan:
        plan = runtime.agent.plan(goal)
        if not plan.get("ok"):
            print(f"planner failed: {plan.get('error')}")
        else:
            print(f"\n{plan.get('summary', goal)}\n")
            for step in plan.get("steps", []):
                flag = " [confirm]" if step.get("needs_confirmation") else ""
                print(f"  {step.get('id')}. {step.get('title')}{flag}")
            print()
    result = runtime.agent.run(goal, session=args.session, allow_tools=not args.no_tools)
    for step in result.steps:
        icon = {"ok": "+", "denied": "!", "error": "x", "needs_confirmation": "?"}.get(step.status, "-")
        print(f"  {icon} {step.action}({_short(step.args)}) -> {step.status}")
        if step.error:
            print(f"      {step.error}")
    print("\n" + result.answer)
    return 0 if result.status in ("ok", "max_steps") else 1


def cmd_chat(runtime: Runtime, args: argparse.Namespace) -> int:
    print(BANNER)
    print(f"workspace: {runtime.config.workspace}   model: {runtime.llm.model}")
    print("commands: /plan <goal>, /status, /audit, /quit\n")
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line == "/status":
            print(json.dumps(runtime.status(), indent=2, default=str))
            continue
        if line == "/audit":
            for entry in runtime.audit.entries(10):
                print(f"  {entry['iso']} {entry['action']:<24} {entry['status']} risk={entry['risk']}")
            continue
        if line.startswith("/plan"):
            goal = line[5:].strip()
            print(json.dumps(runtime.agent.plan(goal), indent=2, default=str))
            continue
        result = runtime.agent.run(line, session=args.session)
        for step in result.steps:
            print(f"  [{step.status}] {step.action}")
        print("\nultron> " + result.answer + "\n")


def cmd_serve(runtime: Runtime, args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from .server.app import Dashboard

    dashboard = Dashboard(runtime)
    if args.host:
        dashboard.host = args.host
    if args.port:
        dashboard.port = args.port
    url = f"http://{dashboard.host}:{dashboard.port}"
    if not args.no_browser and runtime.config.get("server.open_browser", True):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"dashboard: {url}   (ctrl-c to stop)")
    dashboard.serve_forever()
    return 0


def cmd_status(runtime: Runtime) -> int:
    print(json.dumps(runtime.status(), indent=2, default=str))
    return 0


def cmd_doctor(runtime: Runtime) -> int:
    from .core.llm import LLMUnavailable, is_offline
    from .voice import asr as asr_mod
    from .voice import tts as tts_mod

    checks: List[Dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    health = runtime.llm.health()
    check("local model server", bool(health.get("ok")), health.get("error") or health.get("url", ""))
    if health.get("ok") and runtime.llm.model and runtime.llm.model not in health.get("models", []):
        check("configured model present", False,
              f"{runtime.llm.model} not in {len(health.get('models', []))} pulled models")
    else:
        check("configured model present", bool(health.get("ok")), runtime.llm.model)

    workspace = Path(runtime.config.workspace)
    writable = os.access(workspace, os.W_OK) if workspace.exists() else True
    check("workspace writable", writable, str(workspace))
    check("sandbox isolated", str(runtime.policy.sandbox_root) == str(workspace),
          str(runtime.policy.sandbox_root))

    verify = runtime.audit.verify()
    check("audit chain", bool(verify.get("ok")), json.dumps(verify))
    check("vault", True, get_keyring_backend() or "stdlib PBKDF2 + HMAC-CTR file cipher")
    check("speech-to-text", bool(asr_mod.STT(runtime.config).available()),
          ", ".join(asr_mod.STT(runtime.config).available()) or "none installed")
    check("text-to-speech", bool(tts_mod.TTS(runtime.config).available()),
          ", ".join(tts_mod.TTS(runtime.config).available()) or "none installed")
    check("network", is_offline(), "host has internet" if not is_offline() else "host is offline")
    check("offline_only enforced", bool(runtime.llm.offline_only), runtime.llm.base_url)
    check("tools registered", bool(runtime.registry.names()), f"{len(runtime.registry.names())} tools")

    width = max(len(item["check"]) for item in checks)
    for item in checks:
        print(f"  {'PASS' if item['ok'] else 'WARN'}  {item['check']:<{width}}  {item['detail']}")
    failures = sum(1 for item in checks if not item["ok"])
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return 0


def cmd_tools(runtime: Runtime, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(runtime.registry.schemas(), indent=2, default=str))
        return 0
    for tool in runtime.registry.all():
        print(f"  {tool.name:<26} risk={tool.risk:<5} {tool.capability:<10} {tool.description}")
    return 0


def cmd_tasks(runtime: Runtime, args: argparse.Namespace) -> int:
    tasks = runtime.memory.list_tasks(args.n)
    if not tasks:
        print("no tasks yet")
        return 0
    for task in tasks:
        print(f"  {task['id']}  {task['status']:<16} {task['title'][:60]}")
    return 0


def cmd_audit(runtime: Runtime, args: argparse.Namespace) -> int:
    verify = runtime.audit.verify()
    if args.verify:
        print(json.dumps(verify, indent=2))
        return 0 if verify.get("ok") else 2
    for entry in reversed(runtime.audit.entries(args.n)):
        print(f"  {entry['iso']}  {entry['action']:<26} {entry['status']:<8} "
              f"risk={entry['risk']:<5} {json.dumps(entry['detail'], default=str)[:90]}")
    print(f"\nchain: {'intact' if verify.get('ok') else 'BROKEN'} ({verify.get('records')} records)")
    return 0


def cmd_vault(runtime: Runtime, args: argparse.Namespace) -> int:
    from getpass import getpass

    path = runtime.config.path_for("security.vault_file", "~/.ultron/vault.json")
    vault = runtime.vault or Vault(str(path))
    if args.vault_command == "status":
        print(json.dumps({"path": str(path), "exists": vault.exists(),
                          "keyring": get_keyring_backend()}, indent=2))
        return 0
    password = getpass("vault password: ")
    if not vault.unlock(password, create=True):
        print("wrong password")
        return 2
    if args.vault_command == "set":
        vault.set(args.key, args.value)
        print(f"stored {args.key}")
    elif args.vault_command == "get":
        value = vault.get(args.key)
        print(value if value is not None else "not found")
    elif args.vault_command == "list":
        for key in vault.keys():
            print(f"  {key} = {vault.get(key) if args.show else mask(vault.get(key) or '')}")
    vault.lock()
    return 0


def cmd_challan(runtime: Runtime, args: argparse.Namespace) -> int:
    tool = runtime.registry.get("generate_challan")
    assert tool is not None
    data = tool.func(
        challan_type=args.type, amount=args.amount, pan=args.pan, name=args.name,
        assessment_year=args.ay, output_path=args.out,
        financial_year=_fy_from_ay(args.ay) if args.ay else "",
    )
    print(json.dumps(data, indent=2, default=str))
    return 0 if data.get("ok") else 1


def cmd_tax(runtime: Runtime, args: argparse.Namespace) -> int:
    payload = json.load(sys.stdin) if args.input == "-" else json.load(open(args.input))
    tool = runtime.registry.get("income_tax_compute")
    assert tool is not None
    data = tool.func(income=payload.get("income", payload),
                     taxpayer_data=payload.get("taxpayer", {}),
                     assessment_year=args.ay or payload.get("assessment_year", ""),
                     regime=args.regime)
    if args.out:
        doc = runtime.registry.get("income_tax_document")
        assert doc is not None
        data["document"] = doc.func(path=args.out, income=payload.get("income", payload),
                                    taxpayer_data=payload.get("taxpayer", {}),
                                    assessment_year=args.ay, regime=args.regime)
    print(json.dumps(data, indent=2, default=str))
    return 0


def cmd_gst(runtime: Runtime, args: argparse.Namespace) -> int:
    payload = json.load(open(args.input))
    tool = runtime.registry.get("gst_invoice")
    assert tool is not None
    data = tool.func(items=payload.get("items", []),
                     seller_state=payload.get("seller_state", ""),
                     place_of_supply=payload.get("place_of_supply", ""),
                     invoice_number=payload.get("invoice_number", "1"),
                     invoice_date=payload.get("invoice_date", ""),
                     reverse_charge=bool(payload.get("reverse_charge", False)))
    if args.out:
        doc = runtime.registry.get("gst_invoice_document")
        assert doc is not None
        data["document"] = doc.func(path=args.out, items=payload.get("items", []),
                                    seller_state=payload.get("seller_state", ""),
                                    seller_name=payload.get("seller_name", ""),
                                    seller_gstin=payload.get("seller_gstin", ""),
                                    buyer_name=payload.get("buyer_name", ""),
                                    buyer_gstin=payload.get("buyer_gstin", ""),
                                    invoice_number=payload.get("invoice_number", "1"),
                                    invoice_date=payload.get("invoice_date", ""))
    print(json.dumps(data, indent=2, default=str))
    return 0


def cmd_voice(runtime: Runtime, args: argparse.Namespace) -> int:
    from .voice import asr as asr_mod
    from .voice import tts as tts_mod

    if args.action == "backends":
        print(json.dumps({"stt": asr_mod.STT(runtime.config).available(),
                          "tts": tts_mod.TTS(runtime.config).available()}, indent=2))
        return 0
    if args.action == "stt":
        transcript = asr_mod.STT(runtime.config).transcribe(args.target)
        print(transcript.text or f"[no speech] {transcript.error}")
        return 0 if transcript.ok else 1
    speech = tts_mod.TTS(runtime.config).speak(tts_mod.strip_for_speech(args.target))
    print(json.dumps(speech.to_dict(), indent=2))
    return 0 if speech.ok else 1


def _short(args: Any, limit: int = 70) -> str:
    text = json.dumps(args, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _fy_from_ay(ay: str) -> str:
    try:
        start = int(ay[:4]) - 1
    except (TypeError, ValueError):
        return ""
    return f"{start}-{(start + 1) % 100:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
