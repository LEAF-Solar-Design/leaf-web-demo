"""Restricted child process implementation.

The controls here reduce accidental access by trusted code.  They are not
hostile-code isolation and must not be presented as such.
"""
from __future__ import annotations

import builtins
import json
import os
import time
import signal
import traceback
from multiprocessing.connection import Connection
from typing import Any

try:  # resource is unavailable on Windows.
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None  # type: ignore[assignment]


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
        "MemoryError",
    )
    safe = {name: getattr(builtins, name) for name in names}
    safe["__build_class__"] = builtins.__build_class__
    safe["__import__"] = _imports
    safe["open"] = _denied
    return safe


def _lower_resource_limit(name: str, target: int) -> bool:
    """Lower one child-only POSIX limit without raising a host policy limit."""
    if resource is None or not hasattr(resource, name):
        return False
    limit = getattr(resource, name)
    try:
        current, maximum = resource.getrlimit(limit)
        infinity = resource.RLIM_INFINITY
        if maximum != infinity:
            target = min(target, maximum)
        if current != infinity:
            target = min(target, current)
        if current == target:
            return True
        resource.setrlimit(limit, (target, maximum))
        return True
    except (OSError, ValueError):
        return False


def _apply_resource_limits(limits: dict[str, int]) -> None:
    """Apply irreversible per-child limits before the approved source loads."""
    _lower_resource_limit("RLIMIT_AS", limits["max_memory_mb"] * 1024 * 1024)
    _lower_resource_limit("RLIMIT_NPROC", 1)
    _lower_resource_limit("RLIMIT_FSIZE", limits["max_output_bytes"])
    # RLIMIT_CPU is whole seconds.  The interval timer below enforces the
    # catalog's millisecond budget when the platform provides it.
    _lower_resource_limit("RLIMIT_CPU", max(1, (limits["max_cpu_ms"] + 999) // 1000))


def _cpu_exceeded(_signum: int, _frame: Any) -> None:
    # Do not let a tool catch a CPU deadline exception and keep the warm child.
    os._exit(75)


def _start_cpu_timer(max_cpu_ms: int) -> tuple[Any, Any] | None:
    if not hasattr(signal, "ITIMER_PROF") or not hasattr(signal, "SIGPROF"):
        return None
    try:
        previous_handler = signal.signal(signal.SIGPROF, _cpu_exceeded)
        previous_timer = signal.setitimer(signal.ITIMER_PROF, max_cpu_ms / 1000)
        return previous_handler, previous_timer
    except (OSError, ValueError):
        return None


def _stop_cpu_timer(state: tuple[Any, Any] | None) -> None:
    if state is None:
        return
    previous_handler, previous_timer = state
    signal.setitimer(signal.ITIMER_PROF, 0)
    signal.signal(signal.SIGPROF, previous_handler)
    if previous_timer[0] or previous_timer[1]:
        signal.setitimer(signal.ITIMER_PROF, *previous_timer)


def child_main(conn: Connection) -> None:
    """Handle load/invoke/clear commands without any parent credentials."""
    os.environ.clear()
    bindings: dict[str, tuple[Any, dict[str, Any], dict[str, int], int]] = {}
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
                limits = command["limits"]
                _apply_resource_limits(limits)
                exec(compile(command["source"], "<approved-artifact>", "exec"), namespace, namespace)
                runner = namespace.get("run")
                if not callable(runner):
                    raise TypeError("approved source must export run(intake, params)")
                bindings[assignment_id] = (runner, command["drawing_context"], limits, 1)
                conn.send({"ok": True, "loaded": True})
            except Exception as exc:  # source validation error only, never expose trace to caller
                conn.send({"ok": False, "error": f"source load failed: {type(exc).__name__}: {exc}"})
            continue
        if action == "invoke":
            binding = bindings.get(command["assignment_id"])
            if binding is None:
                conn.send({"ok": False, "error": "assignment is not loaded"})
                continue
            runner, drawing_context, limits, loads = binding
            tool_calls = 0

            def tool_call() -> None:
                nonlocal tool_calls
                tool_calls += 1
                if tool_calls > limits["max_tool_calls"]:
                    raise RuntimeError("tool call count exceeds configured limit")

            timer = _start_cpu_timer(limits["max_cpu_ms"])
            cpu_started = time.process_time_ns()
            try:
                intake = {"drawing_context": drawing_context, "tool_call": tool_call}
                returned = runner(intake, command["params"])
                result, overlay = returned if isinstance(returned, tuple) and len(returned) == 2 else (returned, None)
                if not isinstance(result, dict):
                    result = {"value": result}
                payload = {"result": result}
                if overlay is not None:
                    payload["overlay"] = overlay
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if len(encoded) > limits["max_output_bytes"]:
                    conn.send({"ok": False, "error": "tool output exceeds configured limit"})
                    continue
                conn.send({"ok": True, "payload": payload, "bytes": len(encoded), "loads": loads,
                           "cpu_ms": max(0, (time.process_time_ns() - cpu_started) // 1_000_000),
                           # Process high-water RSS is not per-invocation usage.
                           # Keep memory nonbillable until a trusted per-call meter exists.
                           "memory_peak_bytes": 0, "tool_calls": tool_calls})
            except MemoryError:
                # A memory-limit failure leaves the child unreliable.  Exit so
                # the supervisor replaces this slot only.
                os._exit(76)
            except Exception as exc:
                conn.send({"ok": False, "error": f"tool failed: {type(exc).__name__}: {exc}",
                           "cpu_ms": max(0, (time.process_time_ns() - cpu_started) // 1_000_000),
                           "memory_peak_bytes": 0, "tool_calls": tool_calls})
            finally:
                _stop_cpu_timer(timer)
            continue
        conn.send({"ok": False, "error": "unknown child command"})
