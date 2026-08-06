"""F4-hardened artifact entry resolution: the FILE is the artifact.

Extracted from leaf-web-demo tool_loader.py (security-audit 2026-07-18 F4):
``entry`` references were once honoured verbatim — an absolute path or a ``..``
traversal could name ANY ``.py`` on disk for the credential-holding process to
import and execute. The carried-over rule set:

* ``is_unsafe_ref``: a reference is rejected outright if, under EITHER POSIX or
  Windows path semantics, it is absolute / drive- or root-anchored /
  home-relative (``~``) or contains a ``..`` parent component. Only plain
  relative references are permitted.
* ``resolve_within``: joined onto an ALLOWED root and accepted only if the
  resolved file stays INSIDE that root (symlink-safe) and is a regular ``.py``
  file — a second, belt-and-suspenders containment gate behind
  ``is_unsafe_ref`` (defeats a symlink inside the repo that points back out).
* ``resolve_entry``: a package that DECLARED a local Python body resolves it
  exactly, or the resolution is an honest miss (``None``) — never a silent
  substitution of a different file. Consumers layer their own fallback policy
  (builtin tables, alternate roots) on top; substitution-on-miss is the exact
  re-dispatch this module exists to prevent.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, Optional, Tuple


def is_unsafe_ref(ref: Any) -> bool:
    """True if an ``entry``/``script`` is NOT a plain repo-relative path."""
    if not isinstance(ref, str) or not ref.strip():
        return True
    r = ref.strip()
    if r.startswith("~"):
        return True
    for cls in (PurePosixPath, PureWindowsPath):
        p = cls(r)
        if p.is_absolute() or p.root or p.drive:
            return True
        if any(part == ".." for part in p.parts):
            return True
    return False


def resolve_within(root: Path, rel: str) -> Optional[Path]:
    """Join repo-relative ``rel`` onto an allowed ``root``; return the resolved
    ``.py`` file ONLY if it stays INSIDE ``root`` (symlink-safe) and is a
    regular file. Returns None otherwise.
    """
    try:
        root_r = Path(root).resolve()
        cand = (Path(root) / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if cand == root_r:
        return None  # resolved to the root dir itself, not a file
    try:
        cand.relative_to(root_r)
    except ValueError:
        return None  # escaped the allowed root (traversal / symlink) -> reject
    if cand.suffix == ".py" and cand.is_file():
        return cand
    return None


def declares_local_python(tool: Dict[str, Any]) -> bool:
    """True if this package CLAIMS a local Python body, via ``entry`` or ``script``.

    Case-insensitive: a ``.PY`` declaration is still a Python declaration, and
    on Windows it names the same file as ``.py``.
    """
    for declared in (tool.get("entry"), tool.get("script")):
        if isinstance(declared, str) and declared.strip().lower().endswith(".py"):
            return True
    return False


def resolve_entry(
    tool: Dict[str, Any],
    roots: Iterable[Tuple[Path, str]],
) -> Optional[Path]:
    """Resolve a tool's declared ``entry`` against ordered allowed (root, rel)
    candidates; first containment-checked hit wins. A declared ``.py`` entry
    that resolves nowhere returns None — an honest miss, never a substitution.
    """
    entry = tool.get("entry")
    if entry and not is_unsafe_ref(entry):
        for root, rel in roots:
            hit = resolve_within(root, rel)
            if hit is not None:
                return hit
    return None
