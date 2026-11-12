"""Records every repository path a test process reads, for the suite read-set check.
Inert unless LEAF_READ_AUDIT_DIR is set; it never changes what the code under test sees.
A file opened by C code without these Python entry points, or by direct
io.FileIO(...) construction, is not recorded.
References captured in closures, default arguments, functools.partial objects or
class attributes before install are not rebound; reads through them are not recorded.
An opaque child's declaration rests on argv, cwd and env hints. A program that
finds checkout paths from its own config files, or from env vars outside the git
candidate list for non-git children, or a read through an untracked symlink inside
the checkout, is not seen. The check reports the normalized command so a person
can judge it.
Module attribution is opt-in via LEAF_READ_AUDIT_ATTRIBUTE=module. Consumers
compute static import closures: i:true with m:null is a runner import (including
test files and conftest), covered by that closure, not a global read. An m:null
entry without i is a global read.
pytest finds its ini file before loading -p plugins, so repository pytest config
files belong in run_all.
"""

import atexit
import _io
import builtins
import functools
import glob
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import traceback
import types
import urllib.parse


ENV_DIR = "LEAF_READ_AUDIT_DIR"
ENV_ROOT = "LEAF_READ_AUDIT_ROOT"
ENV_ATTRIBUTE = "LEAF_READ_AUDIT_ATTRIBUTE"
MAX_ENTRIES = 100_000
CACHE_MAX = 200_000
TRACE_MAX = 500_000

_state = None
_fast = set()
_cwd_epoch = 0
_chdir_pending = 0
_fork_registered = False
_local = threading.local()
_SKIP = {".git", "__pycache__", ".pytest_cache"}


def _error(directory, exc):
    """Use exclusive creation so even repeated failures leave one small marker."""
    try:
        fd = os.open(os.path.join(directory, f"{os.getpid()}.error"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            first = f"{type(exc).__name__}: {exc}".replace("\r", "\\r").replace("\n", "\\n")
            first = first.encode("utf-8", errors="backslashreplace")[:4095]
            first = first.decode("utf-8", errors="ignore").encode("utf-8") + b"\n"
            tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).splitlines()
            tail = "\n".join(tail[-20:]).encode("utf-8", errors="backslashreplace")
            remaining = 4096 - len(first)
            tail = tail[-remaining:].decode("utf-8", errors="ignore").encode("utf-8") if remaining else b""
            _write(fd, first + tail)
        finally:
            os.close(fd)
    except Exception:
        pass


def _write(fd, content):
    if os.write(fd, content) != len(content):
        raise OSError("short audit write")


def _relative_to_root(absolute, root):
    try:
        inside = os.path.commonpath((root, absolute)) == root
    except ValueError:
        inside = False
    relative = os.path.relpath(absolute, root).replace(os.sep, "/") if inside else None
    if relative is not None:
        parts = relative.split("/")
        if len(parts) >= 2 and parts[-2] == "__pycache__":
            source = re.fullmatch(r"([^.]+)\..*\.pyc", parts[-1])
            if source is not None:
                relative = "/".join([*parts[:-2], source[1] + ".py"])
    if relative is not None and _SKIP.intersection(relative.split("/")):
        relative = None
    return relative


def _relative(path, base=None, all_paths=False):
    if isinstance(path, int) or path is None:
        return () if all_paths else None
    path = os.fsdecode(os.fspath(path))
    absolute = os.path.abspath(os.path.join(base, path) if base is not None else path)
    cache = _state["cache"]
    cached = cache.get(absolute)
    if cached is None or cached[1]:
        busy = getattr(_local, "busy", False)
        _local.busy = True
        try:
            real = os.path.realpath(absolute)
        finally:
            _local.busy = busy
        linked = real != absolute
        root = _state["root"]
        real_root = _state["real_root"]
        if any(_under(candidate, temp) for candidate in (absolute, real)
               for temp in _state["temp_roots"]):
            if absolute in cache or len(cache) < CACHE_MAX:
                cache[absolute] = ((), linked)
            return () if all_paths else None
        relative = _relative_to_root(absolute, root)
        if relative is None:
            relative = _relative_to_root(absolute, real_root)
        relatives = [relative] if relative is not None else []
        if real != absolute:
            relative = _relative_to_root(real, real_root)
            if relative is not None and relative not in relatives:
                relatives.append(relative)
        relatives = tuple(relatives)
        if absolute in cache or len(cache) < CACHE_MAX:
            cache[absolute] = (relatives, linked)
    else:
        relatives = cached[0]
    return relatives if all_paths else next(iter(relatives), None)


def _under(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _module_relative(filename):
    cache = _state["module_cache"]
    if filename not in cache:
        relative = None
        if filename and not filename.startswith("<") and filename not in {"built-in", "frozen"}:
            try:
                absolute = os.path.abspath(filename)
                real = os.path.realpath(absolute)
                if (real != _state["audit_file"] and
                        not any(_under(path, temp) for path in (absolute, real)
                                for temp in _state["temp_roots"])):
                    relative = _relative_to_root(real, _state["real_root"])
            except (OSError, ValueError):
                pass
        cache[filename] = relative
    return cache[filename]


def _module_context():
    # Module attribution is sound under caching: a cached value reaches a test only through code it imports, so attribute the read to that code, not to the current test.
    frame = sys._getframe(1)
    imported = False
    busy = getattr(_local, "busy", False)
    _local.busy = True
    try:
        # Internal helpers do not consume the wrapper caller's frame budget.
        while frame is not None and frame.f_code.co_filename == __file__:
            frame = frame.f_back
        for _ in range(64):
            if frame is None:
                break
            filename = frame.f_code.co_filename
            imported |= filename.startswith("<frozen importlib")
            relative = _module_relative(filename)
            if relative is not None:
                return relative, imported
            frame = frame.f_back
        return None, imported
    finally:
        del frame
        _local.busy = busy


def _attributed(entry, context):
    entry["m"] = context[0]
    if context[1]:
        entry["i"] = True
    return entry


def _trace(state, entry, key):
    if not state["trace"] or state["trace_overflow"]:
        return
    # Trace attribution reads PYTEST_CURRENT_TEST, which pytest sets per test phase and every thread sees; a module global set by a hookwrapper lost pool-thread reads in one xdist worker (11-07).
    current_test = os.environ.get("PYTEST_CURRENT_TEST")
    test_nodeid = current_test.split(" (", 1)[0] if current_test is not None else None
    trace_key = (test_nodeid, key)
    if trace_key in state["trace_seen"]:
        return
    try:
        overflow = state["trace_count"] >= TRACE_MAX
        record = {"t": None, "k": "o"} if overflow else {"t": test_nodeid, **entry}
        content = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        if state["trace_fd"] is None:
            state["trace_fd"] = os.open(state["trace_path"],
                                        os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        _write(state["trace_fd"], content)
        if overflow:
            state["trace_overflow"] = True
        else:
            state["trace_seen"].add(trace_key)
            state["trace_count"] += 1
    except Exception:
        state["trace"] = False
        _close_trace(state)


def _close_trace(state=None):
    state = _state if state is None else state
    if state is not None and state["trace_fd"] is not None:
        fd, state["trace_fd"] = state["trace_fd"], None
        try:
            os.close(fd)
        except OSError:
            pass


def _emit(entry):
    state = _state
    if state["attribute"] and "m" not in entry:
        _attributed(entry, _module_context())
    key = ((entry["k"], entry["p"]) if entry["k"] in {"f", "d", "e"}
           else (entry["exe"], entry["cwd"], entry.get("sub"),
                 tuple(entry.get("opts", [])), tuple(entry["paths"]),
                 tuple(entry.get("argv", []))))
    if state["attribute"]:
        key = (*key, entry["m"])
    with state["lock"]:
        if state["trace"]:
            _trace(state, entry, key)
        if state["overflow"] or key in state["seen"]:
            return
        if state["count"] >= MAX_ENTRIES:
            _write(state["fd"], b'{"k": "o"}\n')
            state["overflow"] = True
            return
        content = json.dumps(entry, sort_keys=True, ensure_ascii=True)
        _write(state["fd"], (content + "\n").encode("utf-8"))
        state["seen"].add(key)
        state["count"] += 1


def _path(kind, path, dir_fd=None, context=None):
    if _state["attribute"] and context is None:
        context = _module_context()
    epoch = _cwd_epoch
    absolute = (os.path.abspath(os.fsdecode(os.fspath(path)))
                if dir_fd is None and path is not None and not isinstance(path, int) else None)
    base = None
    if dir_fd is not None and path is not None and not isinstance(path, int):
        if not os.path.isabs(os.fsdecode(os.fspath(path))):
            try:
                base = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                return
    resolved = (os.path.join(base, os.fsdecode(os.fspath(path))) if base is not None else
                path if dir_fd is not None else absolute)
    for relative in _relative(resolved, None, True):
        entry = {"k": kind, "p": relative}
        _emit(_attributed(entry, context) if context is not None else entry)
    key = _fast_key(kind, path, dir_fd, context)
    if key is not None and _cwd_epoch == epoch and len(_fast) < CACHE_MAX:
        cached = _state["cache"].get(absolute)
        if cached is not None and not cached[1]:
            _fast.add(key)


def _fast_key(kind, raw, dir_fd=None, context=None):
    # Fast path covers Path objects and relative paths too; a relative key carries the cwd epoch so a chdir can never reuse a stale key. improver measured +30 percent CPU without it.
    if dir_fd is not None:
        return None
    if type(raw) is not str and type(raw) is not bytes and isinstance(raw, os.PathLike):
        try:
            raw = os.fspath(raw)
        except Exception:
            return None
    if isinstance(raw, (str, bytes)):
        if os.path.isabs(raw):
            key = (kind, raw)
        else:
            if _chdir_pending:
                return None
            key = (kind, raw, _cwd_epoch)
        return (*key, context[0]) if context is not None else key
    return None


def _guard(function, *args):
    state = _state
    if state is None or state["pid"] != os.getpid() or getattr(_local, "busy", False):
        return
    _local.busy = True
    try:
        with state["lock"]:
            function(*args)
    except Exception as exc:
        _error(state["directory"], exc)
    finally:
        _local.busy = False


def _arguments(args):
    if isinstance(args, (str, bytes)):
        value = os.fsdecode(args)
        try:
            return shlex.split(value)
        except ValueError:
            return [value]
    return [os.fsdecode(os.fspath(arg)) for arg in args]


def _isolated(arguments):
    index = 1
    while index < len(arguments):
        token = arguments[index]
        if token in {"--", "-"} or not token.startswith("-"):
            break
        index += 1
        if token.startswith("--"):
            continue
        for offset, option in enumerate(token[1:], start=1):
            if option in "IES":
                return True
            if option in "cm":
                return False
            if option in "WX":
                if offset + 1 == len(token):
                    index += 1
                break
    return False


def _child_resolve(token, base):
    try:
        return os.path.realpath(os.path.join(base, token))
    except (OSError, ValueError):
        return None


def _child_location(location):
    inside = (location is not None and any(
        location == root or location.startswith(root + os.sep)
        for root in (_state["root"], _state["real_root"])))
    return inside and not any(_under(location, temp) for temp in _state["temp_roots"])


def _child_tokens(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&`$")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return [*shlex.split(command), *lexer]
    except ValueError:
        return [command]


def _child_embedded(token):
    roots = sorted({_state["root"], _state["real_root"]}, key=len, reverse=True)
    pattern = "(?:" + "|".join(re.escape(root) for root in roots) + r")(?=$|[/\\])[^'\",()\[\]\s`;&|<>]*"
    return re.findall(pattern, token)


def _child_argv(arguments, base, name=None):
    if name is None:
        name = os.path.basename(arguments[0]) if arguments else ""
        if re.fullmatch(r"python[0-9.]*(?:\.exe)?", name, re.IGNORECASE):
            name = "python"
    normalized = []
    roots = sorted({_state["real_root"], _state["root"]}, key=len, reverse=True)
    replacements = {temp: "<tmp>" for temp in _state["argv_temp_roots"]}
    replacements.update({root: "root:" for root in roots})
    locations = sorted(replacements, key=len, reverse=True)
    pattern = "(" + "|".join(re.escape(root) for root in locations) + r")(?=$|[/\\])([^'\",()\[\]\s`;&|<>]*)"

    def replace_location(match):
        root, suffix = match[1], match[2]
        if replacements[root] == "root:":
            return "root:" + suffix
        # A temp path keeps only its basename: pytest's per-run directories (pytest-N, popen-gwN, test_x0) would make an argv identity differ on every run.
        return "<tmp>/" + re.split(r"[/\\]", suffix)[-1] if suffix else "<tmp>"

    for index, token in enumerate(arguments):
        if index == 0:
            normalized.append(name)
            continue
        original = token
        # Longest root first: a checkout under /tmp stays root:, and a TMPDIR inside the checkout (offhost seats) stays <tmp>.
        token = re.sub(pattern, replace_location, token)
        token = re.sub(r"(^|[\s'\"=,(\[`$;|&])((?:[A-Za-z]:[\\/]|/)[^'\",\s()\[\]`;&|<>]*)",
                       lambda match: match[1] + "<ext>/" + os.path.basename(match[2]), token)
        if token and token == original:
            absolute = os.path.normpath(os.path.join(base, token))
            if os.path.lexists(absolute):
                for root in roots:
                    if absolute == root or absolute.startswith(root + os.sep):
                        token = "root:" + absolute[len(root):]
                        break
        normalized.append(token)
    capped = [token[:400] for token in normalized[:64]]
    remainder = ([token[400:] for token in normalized[:64]], normalized[64:])
    if any(remainder[0]) or remainder[1]:
        digest = hashlib.sha256(json.dumps(remainder, ensure_ascii=True).encode("utf-8")).hexdigest()
        capped.append("sha256:" + digest)
    return capped


def _child(program, args, cwd, env, extra_paths=(), shell=False, command_argv=None,
           uncertain_cwd=False):
    if isinstance(args, (str, bytes)):
        command = os.fsdecode(args)
        try:
            arguments = shlex.split(command)
        except ValueError:
            program = command
            arguments = [command]
    else:
        arguments = _arguments(args)
    if program is None:
        if not arguments:
            return
        program = arguments[0]
    name = os.path.basename(os.fsdecode(os.fspath(program)))
    effective_env = {os.fsdecode(key): os.fsdecode(value)
                     for key, value in (os.environ if env is None else env).items()}
    cwd = None if cwd is None else os.fsdecode(os.fspath(cwd))
    if cwd is not None and os.path.isabs(cwd):
        base = _child_resolve(cwd, "")
    else:
        try:
            current = os.getcwd()
        except OSError:
            current = None
        base = current if cwd is None else _child_resolve(cwd, current) if current is not None else None
    if base is None:
        base = os.path.sep
        uncertain_cwd = True
    if command_argv is None:
        command_argv = list(arguments)
    argv_base = base
    if arguments and os.fsdecode(os.fspath(program)) != arguments[0]:
        extra_paths = (*extra_paths, os.fsdecode(os.fspath(program)))
    if name == "env" and not shell:
        index = 1
        redispatch = True
        env_candidates = list(extra_paths)
        while index < len(arguments):
            token = arguments[index]
            if token.startswith(("-S", "--split-string")):
                if token in {"-S", "--split-string"}:
                    if index + 1 >= len(arguments):
                        redispatch = False
                        break
                    value = arguments[index + 1]
                    end = index + 2
                else:
                    value = token[2:] if token.startswith("-S") else token[len("--split-string"):]
                    value = value.removeprefix("=")
                    end = index + 1
                try:
                    split = shlex.split(value)
                except ValueError:
                    redispatch = False
                    break
                env_candidates.append(value)
                arguments[index:end] = split
                continue
            if token in {"-i", "--ignore-environment", "-"}:
                effective_env.clear()
            elif token in {"-u", "--unset", "-C", "--chdir"} and index + 1 < len(arguments):
                index += 1
                env_candidates.append(arguments[index])
                if token in {"-C", "--chdir"}:
                    resolved = _child_resolve(arguments[index], base)
                    if resolved is None:
                        redispatch = False
                        break
                    base = resolved
                else:
                    effective_env.pop(arguments[index], None)
            elif token.startswith(("-C", "-u")) and len(token) > 2:
                value = token[2:]
                env_candidates.append(value)
                if token.startswith("-C"):
                    resolved = _child_resolve(value, base)
                    if resolved is None:
                        redispatch = False
                        break
                    base = resolved
                else:
                    effective_env.pop(value, None)
            elif token.startswith("--unset="):
                effective_env.pop(token.partition("=")[2], None)
            elif token.startswith("--chdir="):
                value = token.partition("=")[2]
                env_candidates.append(value)
                resolved = _child_resolve(value, base)
                if resolved is None:
                    redispatch = False
                    break
                base = resolved
            elif token == "--":
                index += 1
                break
            elif token.startswith("-"):
                redispatch = False
                break
            else:
                break
            index += 1
        assignments = []
        while index < len(arguments) and re.match(r"[A-Za-z_][A-Za-z_0-9]*=", arguments[index]):
            key, _, value = arguments[index].partition("=")
            effective_env[key] = value
            assignments.append(arguments[index])
            index += 1
        if redispatch and index < len(arguments):
            _child(None, arguments[index:], base, effective_env,
                   (*env_candidates, *assignments), command_argv=command_argv,
                   uncertain_cwd=uncertain_cwd)
            return
    # A child is audited only if it will load the wrappers: same audit env and .site on its PYTHONPATH, and no -I, -E or -S. Anything else is opaque, so the check makes a person declare it.
    if not shell and name.lower().startswith("python"):
        inherited = (effective_env.get(ENV_DIR) and effective_env.get(ENV_ROOT)
                     and any(os.path.normpath(os.path.join(base, path)) == _state["site"]
                             for path in effective_env.get("PYTHONPATH", "").split(os.pathsep)))
        if inherited and not uncertain_cwd and not _isolated(arguments):
            return
    git = name == "git" and not shell
    sub = None
    opts = []
    locations = []
    option_hints = []
    uncertain = uncertain_cwd
    candidates = [*arguments, *extra_paths]
    candidates.extend(effective_env[key] for key in ("GIT_DIR", "GIT_WORK_TREE")
                      if key in effective_env)
    path_candidates = list(candidates)
    if git:
        # git -C <dir> re-roots git: resolve it, or every fixture-repo command in a temp dir reads as the checkout (1,199 false children in improver, 11-07).
        locations = [effective_env[key] for key in ("GIT_DIR", "GIT_WORK_TREE")
                     if key in effective_env]
        env_keys = {"HOME", "XDG_CONFIG_HOME", "EDITOR", "VISUAL", "PAGER", "SSH_ASKPASS",
                    "GIT_SSH", "GIT_SSH_COMMAND", "PATH"}
        candidates.extend(token for key, value in effective_env.items()
                          if key.startswith("GIT_") or key in env_keys
                          for piece in value.split(os.pathsep) if piece
                          for token in _child_tokens(piece) if token)
        uncertain |= any(key in effective_env for key in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"))
        valued = {"-C", "--git-dir", "--work-tree", "-c", "--namespace", "--config-env",
                  "--attr-source", "--super-prefix", "--shallow-file"}
        flags = {"--no-pager", "--paginate", "-p", "-P", "--bare", "--version", "-v",
                 "--help", "-h", "--literal-pathspecs", "--glob-pathspecs",
                 "--noglob-pathspecs", "--icase-pathspecs", "--no-optional-locks",
                 "--no-replace-objects", "--no-lazy-fetch", "--no-advice"}
        index = 1
        while index < len(arguments):
            token = arguments[index]
            option, separator, attached = token.partition("=")
            if option in valued or (token.startswith("-c") and token != "-c"):
                if token.startswith("-c") and not token.startswith("--") and token != "-c":
                    option, separator, attached = "-c", "=", token[2:]
                if separator:
                    value = attached
                    index += 1
                elif index + 1 < len(arguments):
                    value = arguments[index + 1]
                    index += 2
                else:
                    uncertain = True
                    break
                candidates.append(value)
                if option == "-C":
                    resolved = _child_resolve(value, base)
                    if resolved is None:
                        uncertain = True
                    else:
                        base = resolved
                        candidates.append(resolved)
                elif option in {"--git-dir", "--work-tree"}:
                    locations.append(value)
                else:
                    option_hints.append(value)
                    if option in {"-c", "--config-env"}:
                        key, _, config_value = value.partition("=")
                        if option == "--config-env":
                            if config_value not in effective_env:
                                uncertain = True
                            elif effective_env[config_value]:
                                candidates.append(effective_env[config_value])
                        if key.lower() == "core.worktree":
                            uncertain = True
                            locations.append(effective_env.get(config_value, "")
                                             if option == "--config-env" else config_value)
            elif token.startswith("-"):
                if token not in flags:
                    uncertain = True
                index += 1
            else:
                sub = token
                break
        candidates.extend(locations)
        path_candidates = [*arguments[index:], *extra_paths, *option_hints]
        for location in locations:
            if not location:
                continue
            absolute = os.path.abspath(os.path.join(base, location))
            if (os.path.lexists(absolute)
                    or (_SKIP.intersection(absolute.split(os.sep)) and _child_location(absolute))):
                resolved = _child_resolve(location, base)
                if resolved is not None:
                    path_candidates.append(resolved)
        opts = sorted({token.partition("=")[0] + ("=" if "=" in token else "")
                       for token in arguments[index + 1:] if token.startswith("-")})
        starting_base = base
        if effective_env.get("GIT_TEMPLATE_DIR"):
            candidates.append(os.path.join(starting_base, effective_env["GIT_TEMPLATE_DIR"]))
        if sub in {"init", "init-db"}:
            operand = None
            operand_options = {"-b", "--initial-branch", "--template", "--separate-git-dir",
                               "--object-format", "--ref-format", "--shared"}
            offset = index + 1
            while offset < len(arguments):
                token = arguments[offset]
                option, separator, value = token.partition("=")
                if token.startswith("-b") and not token.startswith("--") and len(token) > 2:
                    option, separator, value = "-b", "=", token[2:]
                if option in operand_options:
                    if not separator:
                        if offset + 1 >= len(arguments):
                            uncertain = True
                            break
                        value = arguments[offset + 1]
                    if value and option in {"--template", "--separate-git-dir"}:
                        candidates.append(os.path.join(starting_base, value))
                    offset += 1 if separator else 2
                    continue
                if token == "--":
                    operand = arguments[offset + 1] if offset + 1 < len(arguments) else None
                    break
                if not token.startswith("-"):
                    if operand is None:
                        operand = token
                    else:
                        uncertain = True
                elif token not in {"-q", "--quiet", "--bare"}:
                    uncertain = True
                offset += 1
            if operand is not None:
                resolved = _child_resolve(operand, base)
                if resolved is None:
                    uncertain = True
                else:
                    base = resolved
    candidates = list(dict.fromkeys(arg for arg in candidates if arg))
    path_candidates = list(dict.fromkeys(arg for arg in path_candidates if arg))
    tokenized = {arg: _child_tokens(arg) for arg in dict.fromkeys([*candidates, *path_candidates])
                 if re.search(r"[\s'\"\\;&|<>()$`]", arg)}
    for tokens in (candidates, path_candidates):
        tokens.extend(token for arg in list(tokens)
                      for token in tokenized.get(arg, ()))
    embedded = {path for arg in dict.fromkeys([*candidates, *path_candidates])
                for path in _child_embedded(arg)}
    candidates.extend(embedded)
    path_candidates.extend(embedded)
    candidates += [arg.partition("=")[2] for arg in candidates if "=" in arg]
    if git:
        candidates += [arg[index:] for arg in candidates
                       if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2
                       for index in range(2, len(arg))]
        candidates += [arg.partition("=")[2] for arg in candidates if "=" in arg]
        for arg in list(candidates):
            url = re.match(r"[A-Za-z][A-Za-z0-9+.-]*://[^/]*(/.*)", arg)
            remote = re.match(r"[^/:]+:(/.*)", arg)
            if url or remote:
                candidates.append((url or remote)[1])
    candidates = list(dict.fromkeys(arg for arg in candidates if arg and not arg.startswith("-")))
    for arg in candidates:
        absolute = os.path.abspath(os.path.join(base, arg))
        if _SKIP.intersection(absolute.split(os.sep)) and _child_location(absolute):
            path_candidates.append(arg)
    path_candidates += [arg.partition("=")[2] for arg in path_candidates if "=" in arg]
    paths = set()
    for arg in dict.fromkeys(arg for arg in path_candidates if arg):
        if arg.startswith("-"):
            continue
        try:
            absolute = os.path.abspath(os.path.join(base, arg))
            if arg in embedded or os.path.lexists(absolute):
                relatives = _relative(arg, base, True)
                paths.update(relatives)
                if not relatives:
                    # _relative already resolved this existing candidate; reuse its cache.
                    cached = _state["cache"].get(absolute)
                    real = _child_resolve(arg, base) if cached is None or cached[1] else absolute
                    if _child_location(real):
                        paths.add(".")
        except (OSError, ValueError):
            continue
    if name != "git" and any(os.path.basename(arg) == "git"
                       for arg in dict.fromkeys([*candidates, *path_candidates]) if arg):
        paths.add(".")
    relative_cwd = None if uncertain_cwd else _relative(base)
    if relative_cwd is None and _child_location(base):
        paths.add(".")
    if git and not (uncertain or paths or _child_location(base)):
        if not any(_child_location(_child_resolve(arg, base)) for arg in candidates):
            return
    paths = sorted(paths)
    if re.fullmatch(r"python[0-9.]*(?:\.exe)?", name, re.IGNORECASE):
        name = "python"
    entry = {"k": "x", "exe": name, "cwd": relative_cwd, "paths": paths,
             "argv": _child_argv(command_argv, argv_base, name)}
    if git:
        entry["sub"] = sub
        entry["opts"] = opts
    _emit(entry)


def _process(original, process_name):
    name = process_name
    signature = inspect.signature(original) if name == "_execute_child" else None

    def record(args, kwargs):
        if signature is not None:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            program = values["executable"]
            arguments = values["args"]
            if isinstance(arguments, (str, bytes, os.PathLike)):
                arguments = [arguments]
            else:
                arguments = list(arguments)
            if values["shell"]:
                if program is None:
                    program = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
                arguments = [program, "-c", *arguments]
            _child(program, arguments, values["cwd"], values["env"], shell=values["shell"])
        elif name == "system":
            command = os.fsdecode(args[0] if args else kwargs["command"])
            _child("/bin/sh", ["/bin/sh", "-c", command], None, None, shell=True)
        else:
            offset = 1 if name.startswith("spawn") else 0
            program = args[offset] if len(args) > offset else kwargs.get("path", kwargs.get("file"))
            if name.startswith("execl"):
                with_env = name.endswith("e")
                arguments = args[1:-1] if with_env else args[1:]
                env = args[-1] if with_env else None
            else:
                arguments = args[offset + 1] if len(args) > offset + 1 else kwargs.get("argv", kwargs.get("args"))
                with_env = name.endswith("e") or name.startswith("posix_spawn")
                env = (args[offset + 2] if len(args) > offset + 2 else kwargs.get("env")) if with_env else None
            _child(program, arguments, None, env)

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        _guard(record, args, kwargs)
        return original(*args, **kwargs)

    return wrapped


def _import_edges(result, name, globals_, fromlist, level, context):
    try:
        if name is None:
            name = vars(result).get("__name__")
        if level:
            package = (globals_ or {}).get("__package__")
            if package is None:
                package = getattr((globals_ or {}).get("__spec__"), "parent", None)
            name = importlib.util.resolve_name("." * level + name, package)
        module = sys.modules.get(name)
        if not isinstance(module, types.ModuleType):
            return
        modules = [module]
        parent = name.rpartition(".")[0]
        while parent:
            modules.append(sys.modules.get(parent))
            parent = parent.rpartition(".")[0]
        modules.extend(vars(module).get(item) for item in (fromlist or ())
                       if isinstance(item, str))
    except (AttributeError, ImportError, TypeError, ValueError):
        return
    # Import edges are recorded on cache hits too: a dynamic import of a module already in sys.modules reads nothing, and the consumer must still see the edge.
    for module in modules:
        if not isinstance(module, types.ModuleType):
            continue
        cache = _state["import_cache"]
        if module not in cache:
            attributes = vars(module)
            filename = attributes.get("__file__")
            if not filename:
                filename = getattr(attributes.get("__spec__"), "origin", None)
            relative = _module_relative(filename) if filename else None
            cache[module] = (relative,) if relative is not None else ()
        for relative in cache[module]:
            key = ("f", relative, context[0])
            if not _state["trace"] and key in _state["seen"]:
                continue
            _emit({"k": "f", "p": relative, "m": context[0], "i": True})


def _import_wrap(original, module_call=False):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        state = _state
        if (state is None or state["pid"] != os.getpid() or
                getattr(_local, "busy", False)):
            return result
        name = args[0] if args else kwargs.get("name", "")
        if module_call:
            # import_module returns the fully resolved module, including relatives.
            globals_, fromlist, level = None, (), 0
            name = None
        else:
            globals_ = args[1] if len(args) > 1 else kwargs.get("globals")
            fromlist = args[3] if len(args) > 3 else kwargs.get("fromlist", ())
            level = args[4] if len(args) > 4 else kwargs.get("level", 0)
        _guard(_import_edges, result, name, globals_, fromlist, level, _module_context())
        return result

    return wrapped


def _chdir(original):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        global _cwd_epoch, _chdir_pending
        _chdir_pending += 1
        _cwd_epoch += 1
        _fast.clear()
        try:
            return original(*args, **kwargs)
        finally:
            _cwd_epoch += 1
            _fast.clear()
            _chdir_pending -= 1

    return wrapped


def _rebind(replacements):
    by_id = {id(original): (original, wrapped) for original, wrapped in replacements}
    for cls in dict.fromkeys((getattr(glob, "_StringGlobber", None),
                              getattr(glob, "_Globber", None),
                              getattr(Path, "_globber", None))):
        if cls is not None:
            for name in ("scandir", "lstat", "lexists"):
                value = getattr(cls, name, None)
                pair = by_id.get(id(value))
                if pair is not None and value is pair[0]:
                    setattr(cls, name, staticmethod(pair[1]))
    for module in list(sys.modules.values())[:20_000]:
        if not isinstance(module, types.ModuleType):
            continue
        try:
            attributes = vars(module)
            if attributes is globals():
                continue
            for name, value in list(attributes.items()):
                pair = by_id.get(id(value))
                if pair is not None and value is pair[0]:
                    setattr(module, name, pair[1])
        except Exception as exc:
            _error(_state["directory"], exc)


class _Entry:
    def __init__(self, entry, dir_fd=None):
        self._entry = entry
        self._dir_fd = dir_fd

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def __fspath__(self):
        return self._entry.path

    def stat(self, *, follow_symlinks=True):
        context = _module_context() if _state is not None and _state["attribute"] else None
        if (_state is not None and not _state["trace"] and
                _fast_key("f", self._entry.path, self._dir_fd, context) in _fast):
            return self._entry.stat(follow_symlinks=follow_symlinks)
        _guard(_path, "f", self._entry.path, self._dir_fd, context)
        return self._entry.stat(follow_symlinks=follow_symlinks)


class _Scandir:
    def __init__(self, iterator, dir_fd=None):
        self._iterator = iterator
        self._dir_fd = dir_fd

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._iterator)
        try:
            return _Entry(entry, self._dir_fd)
        except Exception as exc:
            if _state is not None:
                _guard(_error, _state["directory"], exc)
            return entry

    def close(self):
        return self._iterator.close()

    def __enter__(self):
        self._iterator.__enter__()
        return self

    def __exit__(self, *args):
        return self._iterator.__exit__(*args)


def _wrap(original, scandir=False, opening=False, kind="f", sqlite=False, import_read=False):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        if getattr(_local, "busy", False):
            return original(*args, **kwargs)
        context = _module_context() if _state is not None and _state["attribute"] else None
        if context is not None and import_read:
            context = (context[0], True)
        path = args[0] if args else kwargs.get(
            "path", kwargs.get("file", kwargs.get("database", kwargs.get("self"))))
        if kind == "d" and path is None:
            path = "."
        if sqlite and path is not None:
            database = os.fsdecode(os.fspath(path))
            if database == ":memory:" or (database.startswith("file:") and
                                           "mode=memory" in database.partition("?")[2].split("&")):
                return original(*args, **kwargs)
            if kwargs.get("uri", args[7] if len(args) > 7 else False) and database.startswith("file:"):
                # A SQLite URI names a file: strip the scheme and query, or a temp database reads as a new checkout path on every run.
                path = urllib.parse.unquote(urllib.parse.urlsplit(database).path)
                if not path:
                    return original(*args, **kwargs)
        if opening:
            state = _state
            if state is None or state["pid"] != os.getpid():
                return original(*args, **kwargs)
            if state["trace"] or _fast_key(kind, path, kwargs.get("dir_fd"), context) not in _fast:
                _guard(_path, kind, path, kwargs.get("dir_fd"), context)
            # Never hold the audit lock across a real call: a blocking open (FIFO) or a lazy import inside it deadlocks other threads (#5119 review).
            busy = getattr(_local, "busy", False)
            _local.busy = True
            try:
                return original(*args, **kwargs)
            finally:
                _local.busy = busy
        if ((_state is not None and _state["trace"]) or
                _fast_key(kind, path, kwargs.get("dir_fd"), context) not in _fast):
            _guard(_path, kind, path, kwargs.get("dir_fd"), context)
        if kind == "e":
            busy = getattr(_local, "busy", False)
            _local.busy = True
            try:
                return original(*args, **kwargs)
            finally:
                _local.busy = busy
        result = original(*args, **kwargs)
        if scandir:
            try:
                return _Scandir(result, path if isinstance(path, int) else None)
            except Exception as exc:
                if _state is not None:
                    _guard(_error, _state["directory"], exc)
        return result

    for support in (os.supports_fd, os.supports_dir_fd, os.supports_follow_symlinks,
                    os.supports_effective_ids):
        if original in support:
            support.add(wrapped)
    return wrapped


def _site(directory):
    site = os.path.join(directory, ".site")
    os.makedirs(site, exist_ok=True)
    source = (
        "import importlib.machinery, importlib.util, os, sys\n"
        f"_site = {site!r}\n"
        f"_spec = importlib.util.spec_from_file_location('_leaf_read_audit', {os.path.abspath(__file__)!r})\n"
        "_audit = importlib.util.module_from_spec(_spec)\n"
        "sys.modules['_leaf_read_audit'] = _audit\n"
        "sys.modules.setdefault('read_audit', _audit)\n"
        "_spec.loader.exec_module(_audit)\n"
        "_audit.install()\n"
        "_paths = [p for p in sys.path if os.path.abspath(p) != _site]\n"
        "_next = importlib.machinery.PathFinder.find_spec('sitecustomize', _paths)\n"
        "if _next is not None and _next.origin is not None:\n"
        "    _spec = importlib.util.spec_from_file_location('_leaf_next_sitecustomize', _next.origin, "
        "submodule_search_locations=_next.submodule_search_locations)\n"
        "    _module = importlib.util.module_from_spec(_spec)\n"
        "    sys.modules['_leaf_next_sitecustomize'] = _module\n"
        "    _spec.loader.exec_module(_module)\n"
    )
    try:
        fd = os.open(os.path.join(site, "sitecustomize.py"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            _write(fd, source.encode("utf-8"))
        finally:
            os.close(fd)
    existing = os.environ.get("PYTHONPATH", "")
    paths = existing.split(os.pathsep) if existing else []
    if site not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([site, *paths])


def _open_log(directory, pid):
    fd = os.open(os.path.join(directory, f"{pid}-{os.urandom(4).hex()}.jsonl"),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    try:
        _write(fd, (json.dumps({"v": 1, "pid": pid, "argv": sys.argv[:3]}) + "\n").encode("utf-8"))
    except Exception:
        os.close(fd)
        raise
    return fd


def _after_fork():
    global _state, _cwd_epoch, _chdir_pending
    _fast.clear()
    _cwd_epoch = 0
    _chdir_pending = 0
    state = _state
    if state is None:
        return
    fd = None
    _local.busy = True
    try:
        _close_trace(state)
        pid = os.getpid()
        fd = _open_log(state["directory"], pid)
        os.close(state["fd"])
        _state = {**state, "pid": pid, "fd": fd, "lock": threading.RLock(),
                  "seen": set(), "count": 0, "overflow": False,
                  "trace": os.environ.get("LEAF_READ_AUDIT_TRACE") == "1",
                  "trace_path": os.path.join(state["directory"], f"{pid}-{os.urandom(4).hex()}.trace"),
                  "trace_fd": None,
                  "trace_seen": set(), "trace_count": 0, "trace_overflow": False}
    except Exception as exc:
        _error(state["directory"], exc)
        if fd is not None:
            os.close(fd)
        _state = None
    finally:
        _local.busy = False


def install() -> bool:
    """Enable this process and arrange for ordinary Python children to enable it."""
    global _state, _fork_registered
    # Exactly one audit per process: sitecustomize and pytest's -p import the same file under two names, and two installs deadlocked on two locks (11-07).
    owner = getattr(sys, "_leaf_read_audit_owner", None)
    if owner is not None and owner.__dict__ is not globals():
        state = owner._state
        if state is not None and state["pid"] == os.getpid():
            return True
    directory = os.environ.get(ENV_DIR)
    root = os.environ.get(ENV_ROOT)
    if not directory or not root:
        return False
    if _state is not None and _state["pid"] == os.getpid():
        return True
    directory = os.path.abspath(directory)
    fd = None
    _local.busy = True
    try:
        _fast.clear()
        # Test scratch is not a repository input: a temp dir inside the checkout (offhost seats) must not turn every tmp_path file into a read; a temp root that is the checkout or contains it is ignored, or the audit would record nothing.
        temp_roots = {directory, tempfile.gettempdir()}
        temp_roots.update(os.environ[name] for name in ("TMPDIR", "TEMP", "TMP")
                          if os.environ.get(name))
        temp_roots = {resolved for path in temp_roots
                      for resolved in (os.path.abspath(path), os.path.realpath(path))}
        argv_temp_roots = set(temp_roots)
        checkout_roots = (os.path.abspath(root), os.path.realpath(root))
        temp_roots = {candidate for candidate in temp_roots
                      if not any(_under(checkout, candidate) for checkout in checkout_roots)}
        os.makedirs(directory, exist_ok=True)
        pid = os.getpid()
        fd = _open_log(directory, pid)
        _site(directory)
        try:
            import sqlite3
        except ImportError:
            sqlite3 = None
        replacements = []

        def wrap(module, name, factory=_wrap, **options):
            original = getattr(module, name)
            wrapped = next((new for old, new in replacements if old is original), None)
            if wrapped is None:
                wrapped = factory(original, **options)
                replacements.append((original, wrapped))
            setattr(module, name, wrapped)

        # No sys.addaudithook: a hook runs on every audited operation, including id() inside deepcopy (+88 percent measured). Wrap the entry points and rebind references other modules captured before install.
        for module in (builtins, io, _io):
            wrap(module, "open", opening=True)
        for module in (io, _io):
            wrap(module, "open_code", opening=True, import_read=True)
        attribute = os.environ.get(ENV_ATTRIBUTE) == "module"
        if attribute:
            wrap(builtins, "__import__", factory=_import_wrap)
            wrap(importlib, "__import__", factory=_import_wrap)
            wrap(importlib, "import_module", factory=_import_wrap, module_call=True)
        for name in ("stat", "lstat", "access", "readlink"):
            wrap(os, name)
        wrap(os, "open", opening=True)
        wrap(os, "listdir", kind="d")
        wrap(os, "scandir", scandir=True, kind="d")
        if sqlite3 is not None:
            wrap(sqlite3, "connect", opening=True, sqlite=True)
        wrap(subprocess.Popen, "_execute_child", factory=_process, process_name="_execute_child")
        for name in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe",
                     "execl", "execle", "execlp", "execlpe", "spawnv", "spawnve",
                     "spawnvp", "spawnvpe", "system"):
            if hasattr(os, name):
                wrap(os, name, factory=_process, process_name=name)
        for name in ("chdir", "fchdir"):
            if hasattr(os, name):
                wrap(os, name, factory=_chdir)
        # exists/is_* return only existence and type, so a content edit cannot change them; record them as e, which selects on add, delete, rename and type change only.
        for name in ("exists", "lexists", "isfile", "isdir", "islink"):
            wrap(os.path, name, kind="e")
        for name in ("exists", "is_file", "is_dir", "is_symlink"):
            wrap(Path, name, kind="e")
        # pathlib retains its globber class even if glob._StringGlobber is replaced.
        for cls in dict.fromkeys((getattr(glob, "_StringGlobber", None),
                                  getattr(glob, "_Globber", None),
                                  getattr(Path, "_globber", None))):
            if cls is not None:
                for name in ("scandir", "lstat", "lexists"):
                    original = getattr(cls, name, None)
                    wrapped = next((new for old, new in replacements if old is original), None)
                    if wrapped is not None:
                        setattr(cls, name, staticmethod(wrapped))
        _state = {"pid": pid, "root": os.path.abspath(root), "directory": directory,
                  "real_root": os.path.realpath(root), "site": os.path.join(directory, ".site"),
                  "fd": fd, "seen": set(), "cache": {}, "count": 0,
                  "overflow": False, "lock": threading.RLock(), "temp_roots": temp_roots,
                  "argv_temp_roots": argv_temp_roots,
                  "attribute": attribute, "module_cache": {}, "import_cache": {},
                  "audit_file": os.path.realpath(__file__),
                  "trace": os.environ.get("LEAF_READ_AUDIT_TRACE") == "1",
                  "trace_path": os.path.join(directory, f"{pid}-{os.urandom(4).hex()}.trace"),
                  "trace_fd": None, "replacements": replacements,
                  "trace_seen": set(), "trace_count": 0, "trace_overflow": False}
        sys._leaf_read_audit_owner = sys.modules[__name__]
        _rebind(replacements)
        atexit.register(_close_trace)
        if not _fork_registered and hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=_after_fork)
            _fork_registered = True
        return True
    except Exception as exc:
        _error(directory, exc)
        if fd is not None:
            os.close(fd)
        return False
    finally:
        _local.busy = False


def pytest_configure(config):
    getattr(sys, "_leaf_read_audit_owner", sys.modules[__name__]).install()


# pytest loads initial conftests before pytest_configure, so a -p plugin installs at import or those conftests and everything they import are invisible (strace capture, leaf-web-demo: 461 of 499 suites missed a conftest).
if __name__ == "read_audit" and os.environ.get(ENV_DIR):
    try:
        install()
    except Exception as exc:
        _error(os.environ[ENV_DIR], exc)
