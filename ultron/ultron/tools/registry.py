"""Tool registry.

Each tool is a function with a JSON schema and a risk/capability profile.
The agent may only call registered tools, and every call is gated by the
policy engine before it runs.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    parameters: Dict[str, Any]
    risk: float = 0.1
    capability: str = "fs.read"
    category: str = "general"
    confirm_args: List[str] = field(default_factory=list)

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "capability": self.capability,
            "category": self.category,
        }

    def signature_text(self) -> str:
        params = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        rendered = ", ".join(
            f"{name}{'' if name in required else '?'}: {info.get('type', 'string')}"
            for name, info in params.items()
        )
        return f"{self.name}({rendered}) - {self.description}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        risk: float = 0.1,
        capability: str = "fs.read",
        category: str = "general",
        confirm_args: Optional[List[str]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register(Tool(name, description, func, parameters, risk,
                                capability, category, confirm_args or []))
            return func
        return decorator

    def function_tool(self, func: Callable[..., Any], *, name: Optional[str] = None,
                      risk: float = 0.1, capability: str = "fs.read",
                      category: str = "general",
                      confirm_args: Optional[List[str]] = None) -> Tool:
        """Register a plain function, deriving the schema from its signature."""
        doc = inspect.getdoc(func) or ""
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for param in inspect.signature(func).parameters.values():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            properties[param.name] = {"type": _json_type(param.annotation)}
            if param.default is inspect.Parameter.empty:
                required.append(param.name)
        return self.register(Tool(
            name or func.__name__, doc.split("\n")[0] or (name or func.__name__),
            func, {"type": "object", "properties": properties, "required": required},
            risk, capability, category, confirm_args or [],
        ))

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[Tool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def disabled(self, names: List[str]) -> None:
        for name in names or []:
            self._tools.pop(name, None)

    def prompt_block(self) -> str:
        lines = [tool.signature_text() for tool in self.all()]
        return "\n".join(lines)

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self.all()]

    def dispatch(self, name: str, arguments: Dict[str, Any]) -> Any:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool '{name}'")
        signature = inspect.signature(tool.func)
        accepted = {k: v for k, v in (arguments or {}).items() if k in signature.parameters}
        unknown = [k for k in (arguments or {}) if k not in signature.parameters]
        result = tool.func(**accepted)
        if unknown:
            return {"result": result, "ignored_arguments": unknown}
        return result


def _json_type(annotation: Any) -> str:
    text = str(annotation)
    if "int" in text:
        return "integer"
    if "float" in text:
        return "number"
    if "bool" in text:
        return "boolean"
    if "list" in text or "List" in text:
        return "array"
    if "dict" in text or "Dict" in text:
        return "object"
    return "string"


def dumps(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
