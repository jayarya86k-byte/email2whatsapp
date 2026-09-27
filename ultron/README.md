# Ultron

A private, offline super agent that runs entirely on your own machine: local models,
local files, local speech, a local dashboard, and a tamper-evident record of everything
it does. Zero runtime dependencies — the standard library is enough.

```
  __      __      _
 /  |    /  \    | |
/ / |   / / \   | |_ _   _ _ __ ___     offline · private · yours
\ \_  | / ___ \ | __| | | | '_ ` _ \
 \___|/_/   \_\_\__|\__,_|_|_| |_| |_|
```

## What it does

| Area | Capability |
| --- | --- |
| Local reasoning | Ollama / llama.cpp / any OpenAI-compatible server on loopback. Hard-blocked from leaving the device. |
| Device control | Filesystem, shell, code execution and project scaffolding, each risk-rated and permission-gated. |
| Documents | Real `.docx`, `.xlsx`, `.pdf`, `.csv` and print-ready HTML, written with `zipfile` + OOXML — no python-docx, no reportlab. |
| Compliance (India) | GST invoices with CGST/SGST/IGST + RCM, HSN summary, e-invoice payload, GSTR-1 and GSTR-3B drafts. |
| Income tax | AY 2025-26 / 2026-27, new (115BAC(1A)) and old regime, 87A rebate with marginal relief, surcharge, cess, 234B/234C interest, advance-tax schedule, regime comparison, TDS estimates. |
| Challans | Major/minor head challan builder with amount-in-words, HTML and PDF output for any authorised bank branch. |
| Voice | Push-to-talk in the browser, offline STT (faster-whisper / whisper.cpp / vosk) and offline TTS (piper / espeak / say / pyttsx3). |
| Dashboard | Loopback HTTP + SSE server, live step stream, approval prompts, audit browser, policy editor, vault. |
| Security | Risk policy engine, sandbox root, protected-path patterns, secret redaction, encrypted credential vault, hash-chained audit log, DNS-rebinding and Host-header guards. |

## Install

```bash
cd ultron
python3 -m ultron init            # writes ~/.ultron/ultron.config.json
python3 -m ultron doctor          # preflight: model, tools, sandbox, voice, audit
```

Python 3.9+ (developed on 3.10). No pip install needed. For a global command:

```bash
pip install -e .                  # provides the `ultron` command
```

### Point it at a local model

```bash
# Ollama (recommended)
ollama serve
ollama pull qwen2.5-coder:14b-instruct
ollama pull llama3.1:8b

# or llama.cpp
./llama-server -m model.gguf --port 8080
#   then set "llm": { "provider": "openai", "base_url": "http://127.0.0.1:8080/v1" }
```

If the model server is down, Ultron says so plainly instead of failing silently.

## Use it

```bash
python3 -m ultron run "summarise every markdown file in this repo into one document"
python3 -m ultron run --plan "prepare my GST 3B draft for July 2026 from the sales CSV"
python3 -m ultron chat                       # interactive REPL
python3 -m ultron serve                      # dashboard at http://127.0.0.1:8765
```

Direct finance commands (no model required — pure computation):

```bash
python3 -m ultron challan income_tax 185000 --pan ABCDE1234F --ay 2026-27 -o challan.html
python3 -m ultron tax income.json --ay 2026-27 --regime new -o working-paper.html
python3 -m ultron gst invoice.json -o invoice.html
python3 -m ultron tools                      # every tool with its risk score
python3 -m ultron audit --verify             # check the hash chain
python3 -m ultron vault set portal_password  # encrypted, local, prompted
```

## Security model

Every tool call passes `PolicyEngine.check()` before it runs:

1. **Risk** — each tool carries a 0.0–1.0 score. Below `require_confirmation_above_risk`
   it runs; between that and `deny_risk_at_or_above` it pauses for approval; at or above
   the deny threshold it is refused.
2. **Capability** — `fs.read`, `fs.write`, `fs.delete`, `shell`, `code.exec`, `network`,
   `secrets`, `system`. Network is off unless you allowlist a host.
3. **Path** — writes are confined to `security.sandbox_root`. Anything outside it asks
   first. `~/.ssh`, `~/.aws`, `**/*.pem`, `**/.env` and friends are refused outright.
4. **Overrides** — `"security": {"overrides": {"run_shell": "deny"}}` per tool, editable
   live from the dashboard.
5. **Command scanning** — `rm -rf /`, `curl … | sh`, fork bombs, `mkfs`, device writes and
   friends are hard-denied at the gate, before the shell is ever reached.

Privacy guarantees:

- `llm.offline_only` refuses any endpoint that is not loopback.
- Every prompt, result and log line passes through redaction (PANs, cards, Aadhaar-like
  numbers, `password …` pairs, token/key patterns).
- The audit log is append-only and hash-chained: edit a line and `ultron audit --verify`
  reports the exact offset.
- Credentials live in an encrypted vault (PBKDF2-SHA256 600k iterations + HMAC-CTR with
  encrypt-then-MAC). If `keyring` is installed, `doctor` reports it as available.
- The dashboard binds to 127.0.0.1, rejects foreign `Host` headers (DNS rebinding) and
  can require `server.session_token` on every mutating call.

**Ultron computes and prepares; it does not submit.** Filing on the CBDT / GST / MCA
portals needs your credentials, a captcha and your final review, so portal submission is
a separate, explicitly confirmed action — it is not wired into the agent loop. Tax figures
produced here are working papers, so have them verified before you file.

## Voice

```bash
python3 -m ultron voice backends       # what is installed
```

* STT: `pip install faster-whisper` (best), or build `whisper.cpp` and point
  `voice.stt.binary` at `whisper-cli`, or `pip install vosk`.
* TTS: install `piper` with a voice in `~/.local/share/piper-voices`, or `espeak-ng`.
* In the dashboard, **🎙 Voice** is push-to-talk (or `Ctrl+Shift+Space`); the clip is
  transcribed on-device and submitted as if you typed it.

## Architecture

```
ultron/
  cli.py              argparse CLI: run / chat / serve / tax / gst / challan / vault / doctor
  config.py           JSON config + env overrides, path expansion
  runtime.py          object graph builder and status snapshot
  core/
    llm.py            local model client, loopback guard, streaming, hashed embeddings
    agent.py          ReAct loop, JSON protocol parser, step gating, resume-after-approval
    memory.py         SQLite messages, facts, tasks, retrieval
  security/
    policy.py         risk bands, capabilities, sandbox, forbidden paths, overrides
    audit.py          hash-chained JSONL log with secret scrubbing
    vault.py          encrypted credential store
  tools/
    registry.py       schema-annotated tool registry
    fs_tools.py       read / write / list / move / delete / search
    shell_tools.py    shell, sandboxed python, scaffolding, test runners, safety scanner
    doc_tools.py      OOXML docx, xlsx, PDF writer, markdown -> doc/html
    finance_tools.py  the agent-facing finance tools
  finance/
    gst.py            invoice computation, GSTR-1 and GSTR-3B drafts
    incometax.py      regimes, 87A, surcharge, cess, interest, TDS, advance tax
    challan.py        head codes, amount-in-words, print layouts
  voice/              asr.py, tts.py adapters
  server/             stdlib HTTP + SSE dashboard and static UI
tests/                49 tests, no network
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers the audit chain (including tamper detection), policy verdicts, vault crypto, the
agent loop (tool use, unknown-tool recovery, deny, approve-and-resume, rejection, secret
redaction), dashboard routes and guards, the multipart parser, GST tax splits, income-tax
regimes and slabs, challans, and the document writers.

## Customising

Everything lives in `~/.ultron/ultron.config.json` (copy
`ultron/ultron.config.example.json`). `ULTRON_MODEL`, `ULTRON_LLM_URL`, `ULTRON_HOST`,
`ULTRON_PORT`, `ULTRON_SESSION_TOKEN` and `ULTRON_OFFLINE_ONLY` override the file.

To teach it your own procedures, put Markdown playbooks in the workspace and say
"follow the GST playbook" — the agent will read them as ordinary files, and the
dashboard's Memory panel stores durable facts (`remember: my PAN is …`) that get injected
into every prompt.

## Known limits

- Model-driven steps need a decent local model and honest tool schemas; a 3B model will
  pick the wrong tool more often than a 14B one.
- Income-tax tables cover AY 2025-26 and 2026-27 and will need editing each Budget.
- Challan head codes are data, not code — verify them against the live portal.
- The stdlib vault cipher is portable and encrypted at rest, but a hardware keystore
  (Keychain / DPAPI / libsecret) is stronger; `doctor` tells you if one is available.
- No portal automation, no CAPTCHA solving, no scheduled background jobs yet.
