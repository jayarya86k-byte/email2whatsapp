"""Shell, code-execution and project-scaffolding tools.

Commands run through a denylist scanner plus the policy engine; nothing is
executed unless the policy verdict is allow or the user confirmed it.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List

from .registry import ToolRegistry

DANGEROUS = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-?[rf]{1,2}\s+/(\s|$)", "recursive delete of root"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b[^\n]*\bof=/dev/", "raw device write"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", "power state change"),
    (r"\bchown\s+-R\s+root\b", "ownership change to root"),
    (r"\bchmod\s+-R\s+777\s+/(\s|$)", "world writable root"),
    (r"\bcurl\b[^\n|]*\|\s*(ba)?sh", "remote script execution"),
    (r"\bwget\b[^\n|]*\|\s*(ba)?sh", "remote script execution"),
    (r"\bnc\b[^\n]*\s-e\s", "reverse shell"),
    (r">\s*/dev/sd[a-z]", "raw device write"),
    (r"\bhistory\s+-c\b", "history tampering"),
]

PROJECT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "python_cli": {
        "pyproject.toml": "[project]\nname = \"{name}\"\nversion = \"0.1.0\"\n"
                          "requires-python = \">=3.10\"\ndependencies = []\n",
        "src/{name}/__init__.py": "__version__ = \"0.1.0\"\n",
        "src/{name}/main.py": "def main() -> int:\n    print(\"{name} running\")\n    return 0\n\n\n"
                             "if __name__ == \"__main__\":\n    raise SystemExit(main())\n",
        "tests/test_main.py": "from {name}.main import main\n\n\ndef test_main():\n    assert main() == 0\n",
        "README.md": "# {name}\n\nCreated by Ultron.\n",
    },
    "node_service": {
        "package.json": '{{"name": "{name}", "version": "0.1.0", "private": true, '
                        '"type": "module", "scripts": {{"start": "node src/index.js"}}}}\n',
        "src/index.js": "import { createServer } from 'node:http';\n\n"
                        "const port = process.env.PORT ?? 3000;\n"
                        "createServer((_req, res) => res.end('{name} ok')).listen(port, () =>\n"
                        "  console.log(`{name} listening on ${port}`));\n",
        ".gitignore": "node_modules/\n.env\n",
        "README.md": "# {name}\n\nCreated by Ultron.\n",
    },
    "web_static": {
        "index.html": "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n"
                      "  <title>{name}</title>\n  <link rel=\"stylesheet\" href=\"style.css\">\n"
                      "</head>\n<body>\n  <main id=\"app\">Loading...</main>\n"
                      "  <script src=\"app.js\"></script>\n</body>\n</html>\n",
        "style.css": ":root { color-scheme: dark; }\nbody { font-family: system-ui; margin: 0; }\n",
        "app.js": "document.getElementById('app').textContent = '{name} ready';\n",
    },
    "research_note": {
        "notes.md": "# {name}\n\n## Summary\n\n## Findings\n\n## Next steps\n",
    },
}

LANGUAGES = {
    ".py": "python", ".js": "node", ".ts": "node", ".sh": "bash",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
}


def scan_command(command: str) -> List[str]:
    findings: List[str] = []
    for pattern, label in DANGEROUS:
        if re.search(pattern, command, re.IGNORECASE | re.MULTILINE):
            findings.append(label)
    return findings


def register(registry: ToolRegistry, policy: Any, audit: Any, config: Any) -> None:
    shell_timeout = int(config.get("tools.shell_timeout_seconds", 120))
    allow_shell = bool(config.get("tools.allow_shell", True))

    def run_shell(command: str, cwd: str = "", timeout: int = 0) -> Dict[str, Any]:
        """Run a shell command in the workspace and capture stdout/stderr."""
        if not allow_shell:
            return {"ok": False, "error": "shell tools are disabled in ultron.config.json"}
        findings = scan_command(command)
        if findings:
            audit.record("shell.blocked", status="denied", risk=0.9,
                         detail={"command": command, "findings": findings})
            return {"ok": False, "error": "command blocked by safety scanner",
                    "findings": findings}
        workdir = Path(os.path.expanduser(cwd)) if cwd else policy.sandbox_root
        workdir.mkdir(parents=True, exist_ok=True)
        started = os.times()
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(workdir), timeout=timeout or shell_timeout,
                capture_output=True, text=True, errors="replace", env=self_env(),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timed out after {timeout or shell_timeout}s"}
        audit.record("shell.exec", risk=0.6, capability="shell",
                     detail={"command": command, "cwd": str(workdir),
                             "returncode": proc.returncode})
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": _clip(proc.stdout, policy.max_output),
            "stderr": _clip(proc.stderr, policy.max_output),
            "cwd": str(workdir),
        }

    def run_python(code: str, filename: str = "ultron_snippet.py") -> Dict[str, Any]:
        """Execute a Python snippet in an isolated temp directory."""
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif node.module:
                    names = [node.module]
                for name in names:
                    root = name.split(".")[0]
                    if root in {"os", "subprocess", "shutil", "socket", "ctypes", "multiprocessing"}:
                        audit.record("code.exec", status="denied", risk=0.85,
                                     detail={"module": root})
                        return {"ok": False, "error": f"import of '{root}' is not permitted here"}
        with tempfile.TemporaryDirectory(prefix="ultron-") as tmp:
            script = Path(tmp) / filename
            script.write_text(textwrap.dedent(code), "utf-8")
            try:
                proc = subprocess.run(["python3", str(script)], cwd=tmp, timeout=shell_timeout,
                                      capture_output=True, text=True, errors="replace")
            except FileNotFoundError:
                return {"ok": False, "error": "python3 interpreter not found"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"timed out after {shell_timeout}s"}
        audit.record("code.exec", risk=0.55, capability="code.exec",
                     detail={"lines": len(code.splitlines()), "returncode": proc.returncode})
        return {"ok": proc.returncode == 0, "stdout": _clip(proc.stdout, policy.max_output),
                "stderr": _clip(proc.stderr, policy.max_output), "returncode": proc.returncode}

    def scaffold_project(name: str, template: str = "python_cli", path: str = "") -> Dict[str, Any]:
        """Create a new project skeleton from a built-in template."""
        if template not in PROJECT_TEMPLATES:
            return {"ok": False, "error": f"unknown template '{template}'",
                    "available": sorted(PROJECT_TEMPLATES)}
        root = Path(os.path.expanduser(path)) if path else Path(policy.sandbox_root) / name
        files: List[str] = []
        for relative, content in PROJECT_TEMPLATES[template].items():
            target = root / relative.format(name=name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content.format(name=name), "utf-8")
            files.append(str(target.relative_to(root)))
        audit.record("project.scaffold", risk=0.4, capability="fs.write",
                     detail={"name": name, "template": template, "root": str(root)})
        return {"ok": True, "root": str(root), "template": template, "files": files}

    def run_project_tests(path: str = ".", command: str = "") -> Dict[str, Any]:
        """Detect the project's test command and run it."""
        root = Path(os.path.expanduser(path))
        guesses: List[str] = []
        if command:
            guesses = [command]
        elif (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
            guesses = ["python3 -m pytest -q"]
        elif (root / "package.json").exists():
            guesses = ["npm test --silent"]
        elif (root / "go.mod").exists():
            guesses = ["go test ./..."]
        elif (root / "Cargo.toml").exists():
            guesses = ["cargo test -q"]
        if not guesses:
            return {"ok": False, "error": "no known test runner detected; pass `command`"}
        return run_shell(guesses[0], cwd=str(root))

    def which_tool(binary: str) -> Dict[str, Any]:
        """Report whether a local binary is installed (used by voice backends)."""
        found = shutil.which(binary)
        return {"ok": bool(found), "binary": binary, "path": found}

    if allow_shell:
        registry.function_tool(run_shell, risk=0.6, capability="shell",
                               category="dev", confirm_args=["command"])
    registry.function_tool(run_python, risk=0.55, capability="code.exec",
                           category="dev", confirm_args=["code"])
    registry.function_tool(scaffold_project, risk=0.4, capability="fs.write",
                           category="dev", confirm_args=["name", "path"])
    registry.function_tool(run_project_tests, risk=0.5, capability="shell",
                           category="dev", confirm_args=["path"])
    registry.function_tool(which_tool, risk=0.02, capability="fs.read", category="dev")


def self_env() -> Dict[str, str]:
    """Child processes run with a scrubbed, offline-biased environment."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "*",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "TERM"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...<truncated {len(text) - limit} chars>"
