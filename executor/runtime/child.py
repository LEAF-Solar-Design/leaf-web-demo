"""Restricted child process implementation.

The controls here reduce accidental access by trusted code.  They are not
hostile-code isolation and must not be presented as such.
"""
from __future__ import annotations

import builtins
import json
import os
import traceback
from multiprocessing.connection import Connection
from typing import Any


def _denied(*_args: Any, **_kwargs: Any) -> Any:
    raise PermissionError("restricted executor child denies filesystem and credential access")


def _imports(name: str, *_args: Any, **_kwargs: Any) -> Any:
    if name in {"math", "statistics", "json", "collections"}:
        return builtins.__import__(name, *_args, **_kwargs)
    raise PermissionError(f"restricted executor child denies import {name!r}")


def _safe_builtins() -> dict[str, Any]:
    names = (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
        "frozenset", "getattr", "hasattr", "int", "isinstance", "len", "list",
        "map", "max", "min", "object", "range", "reversed", "round", "set",
        "sorted", "str", "sum", "tuple", "type", "zip", "Exception", "ValueError",
    )
    safe = {name: getattr(builtins, name) for name in names}
    safe["__build_class__"] = builtins.__build_class__
    safe["__import__"] = _imports
    safe["open"] = _denied
    return safe


def child_main(conn: Connection) -> None:
    """Handle load/invoke/clear commands without any parent credentials."""
    os.environ.clear()
    bindings: dict[str, tuple[Any, dict[str, Any], int]] = {}
    while True:
        try:
            command = conn.recv()
        except EOFError:
            return
        action = command.get("action")
        if action == "stop":
            return
        if action == "clear":
            bindings.pop(command["assignment_id"], None)
            conn.send({"ok": True})
            continue
        if action == "load":
            assignment_id = command["assignment_id"]
            if assignment_id in bindings:
                conn.send({"ok": True, "loaded": False})
                continue
            namespace: dict[str, Any] = {"__builtins__": _safe_builtins(), "__name__": "lease_tool"}
            try:
                exec(compile(command["source"], "<approved-artifact>", "exec"), namespace, namespace)
                runner = namespace.get("run")
                if not callable(runner):
                    raise TypeError("approved source must export run(intake, params)")
                bindings[assignment_id] = (runner, command["drawing_context"], 1)
                conn.send({"ok": True, "loaded": True})
            except Exception as exc:  # source validation error only, never expose trace to caller
                conn.send({"ok": False, "error": f"source load failed: {type(exc).__name__}: {exc}"})
            continue
        if action == "invoke":
            binding = bindings.get(command["assignment_id"])
            if binding is None:
                conn.send({"ok": False, "error": "assignment is not loaded"})
                continue
            runner, drawing_context, loads = binding
            try:
                intake = {"drawing_context": drawing_context}
                returned = runner(intake, command["params"])
                result, overlay = returned if isinstance(returned, tuple) and len(returned) == 2 else (returned, None)
                if not isinstance(result, dict):
                    result = {"value": result}
                payload = {"result": result}
                if overlay is not None:
                    payload["overlay"] = overlay
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                conn.send({"ok": True, "payload": payload, "bytes": len(encoded), "loads": loads})
            except Exception as exc:
                conn.send({"ok": False, "error": f"tool failed: {type(exc).__name__}: {exc}"})
            continue
        conn.send({"ok": False, "error": "unknown child command"})
