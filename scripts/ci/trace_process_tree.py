"""Decode a sanitized, receipt-bound Linux process tree without filesystem inference.

Only local input/output filenames reach the host filesystem. Traced names are
POSIX strings interpreted against the launch inventory, never against this host.
Completeness is evidence in the documents, not the decode command's exit status.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote
from dataclasses import dataclass, field

try:
    import resource
except ImportError:  # Windows: the live-state counter stands in for measured RSS.
    resource = None


BINDINGS = ("run_id", "source_sha", "source_tree", "capture_sha", "catalog_sha256")
PARSER_VERSION = "s15a-5"
# max_state_bytes: 2 GiB, compared against measured peak RSS (Amendment 5); the counter it
# replaced only ever added, so a 40 M-line stream tripped it on cumulative allocation.
LIMITS = dict(max_tasks=16384, max_fds_per_task=65536, max_record_bytes=1048576,
              max_dependencies=1000000, max_state_bytes=2 * 1024 ** 3, max_reasons=256)
# ru_maxrss is KiB on Linux, bytes on macOS; sampled once per RSS_INTERVAL lines.
RSS_SCALE, RSS_INTERVAL = (1 if sys.platform == "darwin" else 1024), 65536
# Calibration instrument bounds: distinct sanitized shapes kept, names counted, names reported.
SAMPLE_LIMIT, NAME_LIMIT, NAME_REPORT = 32, 4096, 32
# Raw bytes kept per sampled line; shaping collapses any quoted string the cut leaves open.
SAMPLE_PREFIX = 512
POLICY = {
    # Amendment 4: unconditional-allow, argument-independent syscalls filtered at the tracer
    # (`-e trace=!<noise>`). Digested with the policy and bound into the epoch through the argv.
    "noise": ("brk madvise mprotect munmap arch_prctl set_tid_address set_robust_list get_robust_list rseq rt_sigaction "
              "rt_sigprocmask rt_sigreturn sigaltstack rt_sigsuspend rt_sigtimedwait rt_sigpending restart_syscall "
              "sched_yield nanosleep clock_nanosleep getpid getppid gettid getuid geteuid getgid getegid getgroups "
              "getrlimit prlimit64 getrusage times clock_gettime gettimeofday time getrandom sched_getaffinity "
              "sched_getparam sched_getscheduler futex").split(),
    "path": "open openat creat execve execveat readlink readlinkat access faccessat stat lstat fstat newfstatat statfs fstatfs statx getcwd chdir fchdir getxattr lgetxattr listxattr llistxattr".split(),
    "descriptor": "read pread64 readv preadv preadv2 mmap mremap munmap mprotect lseek dup dup2 dup3 fcntl close pipe pipe2 socketpair write pwrite64 writev pwritev pwritev2 truncate ftruncate fsync fdatasync fadvise64 posix_fadvise readahead sync_file_range flock fgetxattr flistxattr sendfile copy_file_range".split(),
    "metadata_mutation": "chmod fchmod fchmodat chown fchown fchownat lchown utimensat futimesat".split(),
    "internal_object": "memfd_create timerfd_create".split(),
    "filesystem_events": "inotify_init inotify_init1 inotify_add_watch inotify_rm_watch".split(),
    "directory": "getdents getdents64 mkdir mkdirat rmdir unlink unlinkat rename renameat renameat2 link linkat symlink symlinkat".split(),
    "process": "fork vfork clone wait4 waitid exit exit_group setsid setpgid getpid getppid gettid".split(),
    "network": "socket connect bind listen accept accept4 sendto recvfrom sendmsg recvmsg sendmmsg recvmmsg getsockopt setsockopt getpeername getsockname shutdown".split(),
    "namespace": "unshare setns chroot pivot_root mount umount2".split(),
    "reject": "ptrace process_vm_readv process_vm_writev io_setup io_submit io_getevents io_cancel io_destroy io_uring_setup io_uring_enter io_uring_register open_by_handle_at name_to_handle_at splice tee vmsplice bpf userfaultfd shmget shmat shmdt shmctl semget semop semctl msgget msgsnd msgrcv msgctl".split(),
    # faccessat2: glibc's probe, ENOSYS on 4.14 kernels.
    "abi": "openat2 clone3 close_range pidfd_open pidfd_getfd faccessat2".split(),
    "runtime": "brk madvise arch_prctl set_tid_address set_robust_list get_robust_list futex rseq rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack rt_sigsuspend rt_sigtimedwait rt_sigpending restart_syscall kill tkill tgkill sched_yield nanosleep clock_nanosleep poll ppoll select pselect6 epoll_create epoll_create1 epoll_ctl epoll_wait epoll_pwait eventfd eventfd2 alarm setitimer getitimer getuid geteuid getgid getegid getgroups uname getrlimit prlimit64 getrusage times umask clock_gettime clock_getres gettimeofday time getrandom sched_getaffinity sched_getparam sched_getscheduler prctl ioctl sysinfo getpgrp getpgid setrlimit capget sched_setaffinity timer_create timer_settime timer_gettime timer_delete timerfd_settime timerfd_gettime mincore msync mlock munlock".split(),
    "ioctl": "TCGETS TCSETS TCSETSW TCSETSF TIOCGWINSZ TIOCGPGRP TIOCSPGRP FIONBIO FIOCLEX FIONCLEX FIONREAD".split(),
    "prctl": ("PR_SET_NAME PR_GET_NAME PR_SET_PDEATHSIG PR_GET_DUMPABLE PR_SET_DUMPABLE PR_CAPBSET_READ PR_SET_NO_NEW_PRIVS "
              "PR_GET_NO_NEW_PRIVS PR_SET_CHILD_SUBREAPER PR_GET_CHILD_SUBREAPER PR_SET_KEEPCAPS PR_GET_SECCOMP "
              "PR_SET_TIMERSLACK PR_GET_TIMERSLACK PR_SET_PTRACER").split(),
    "fcntl": ("F_DUPFD F_DUPFD_CLOEXEC F_SETFD F_GETFD F_GETFL F_SETFL F_GETLK F_SETLK F_SETLKW F_OFD_GETLK F_OFD_SETLK "
              "F_OFD_SETLKW F_GETPIPE_SZ F_SETPIPE_SZ F_GETOWN F_SETOWN F_GETSIG F_SETSIG F_ADD_SEALS F_GET_SEALS").split(),
    "open_flags": "O_RDONLY O_WRONLY O_RDWR O_APPEND O_ASYNC O_CLOEXEC O_CREAT O_DIRECT O_DIRECTORY O_DSYNC O_EXCL O_LARGEFILE O_NOATIME O_NOCTTY O_NOFOLLOW O_NONBLOCK O_NDELAY O_PATH O_SYNC O_TRUNC O_RSYNC O_TMPFILE".split(),
    "clone_flags": "CLONE_VM CLONE_FS CLONE_FILES CLONE_SIGHAND CLONE_PTRACE CLONE_VFORK CLONE_PARENT CLONE_THREAD CLONE_SYSVSEM CLONE_SETTLS CLONE_PARENT_SETTID CLONE_CHILD_CLEARTID CLONE_DETACHED CLONE_UNTRACED CLONE_CHILD_SETTID CLONE_IO SIGCHLD".split(),
    "nondeterminism_boundary": ["clock", "randomness", "scheduling"],
}
PATH_ARGS = {
    **{n: (0,) for n in "open creat execve readlink access stat lstat statfs chdir truncate mkdir rmdir unlink chroot getxattr lgetxattr listxattr llistxattr chmod chown lchown".split()},
    **{n: (1,) for n in "openat openat2 execveat readlinkat faccessat newfstatat statx mkdirat unlinkat fchmodat fchownat utimensat futimesat".split()},
    "rename": (0, 1), "link": (0, 1), "symlink": (0, 1),
    "renameat": (1, 3), "renameat2": (1, 3), "linkat": (1, 3), "symlinkat": (0, 2),
}
FD_ARGS = {
    **{n: (0,) for n in "read pread64 readv preadv preadv2 write pwrite64 writev pwritev pwritev2 fstat fstatfs fchdir lseek dup fcntl close getdents getdents64 ioctl ftruncate fsync fdatasync connect bind listen accept accept4 sendto recvfrom sendmsg recvmsg sendmmsg recvmmsg getsockopt setsockopt getpeername getsockname shutdown".split()},
    **{n: (0,) for n in "openat openat2 execveat readlinkat faccessat newfstatat statx mkdirat unlinkat".split()},
    **{n: (0,) for n in "fadvise64 posix_fadvise readahead sync_file_range flock fgetxattr flistxattr fchmod fchown fchmodat fchownat utimensat futimesat".split()},
    "dup2": (0, 1), "dup3": (0, 1), "mmap": (4,), "renameat": (0, 2),
    "renameat2": (0, 2), "linkat": (0, 2), "symlinkat": (1,), "epoll_ctl": (0, 2),
    "epoll_wait": (0,), "epoll_pwait": (0,),
    # sendfile(out, in, ...), copy_file_range(in, off, out, ...).
    "sendfile": (0, 1), "copy_file_range": (0, 2),
}
# (in, out) descriptor argument indices of the in-kernel copy calls.
COPIES = {"sendfile": (1, 0), "copy_file_range": (0, 2)}
READS = set("read pread64 readv preadv preadv2".split())
WRITES = set("write pwrite64 writev pwritev pwritev2 ftruncate fsync fdatasync".split())
LOOKUPS = set("stat lstat newfstatat statx access faccessat readlink readlinkat statfs getxattr lgetxattr listxattr llistxattr".split())
MUTATIONS = set(POLICY["directory"]) - {"getdents", "getdents64"}
METADATA_MUTATIONS = set(POLICY["metadata_mutation"])
FD_LOOKUPS = {"fstat", "fstatfs", "fgetxattr", "flistxattr"}
KNOWN = frozenset().union(*(POLICY[k] for k in ("path", "descriptor", "directory", "process", "network", "namespace", "runtime",
                                                "metadata_mutation", "internal_object", "filesystem_events", "noise")))
QUOTED = re.compile(r'"(?:\\.|[^"\\])*(?:"|\\?$)')
# Each alternative stops at the next '<', so the substitution stays linear in the line.
ANNOTATION = re.compile(r"<[^<]*?>(?=[\s,)\]}]|$)|<[^<>]*$")
LEADING = re.compile(r"(?:\[pid +\d+\] |\d+ +)?(?:\d+\.\d+ )?(?:<\.\.\. ([A-Za-z_]\w*) resumed>|([A-Za-z_]\w*)\(|(\+\+\+)|(---)|(strace:))")
# Arguments, return value, optional return annotation, then the tail (aux, errno, duration).
RETURNED = re.compile(r"(.*)\)\s+=\s+(\?|0x[0-9a-fA-F]+|-?\d+)(?:<(.*?)>(?= |$))?(.*)")
# Amendment 5 drops -T: the duration is optional, still accepted when present.
TRAILER = re.compile(r"(?: ([A-Z][A-Z0-9_]+)(?: \([^\n]*\))?)?(?: <(\d+\.\d+|unavailable)>)?")
CLOSERS = {")": "(", "]": "[", "}": "{"}


def _balanced(text, start):
    """End (exclusive) of the bracket group opening at text[start]; quoted strings are skipped.

    Linear in the text; raises malformed_line on a mismatched or unclosed bracket.
    """
    stack, quoted, i = [], False, start
    while i < len(text):
        c = text[i]
        if quoted:
            if c == "\\":
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c in "([{":
            stack.append(c)
        elif c in CLOSERS:
            if not stack or stack.pop() != CLOSERS[c]:
                raise ValueError("malformed_line")
            if not stack:
                return i + 1
        i += 1
    raise ValueError("malformed_line")


def malformed_shape(line):
    """Sanitized shape of an undecodable line: never carries a quoted string's content.

    Quoted strings (terminated or not) collapse first, then annotations, hex, digits;
    truncation happens only after every substitution.
    """
    shape = QUOTED.sub('"…"', line)
    shape = ANNOTATION.sub("<…>", shape)
    shape = re.sub(r"0x[0-9a-fA-F]+", "0xN", shape)
    shape = re.sub(r"\d+", "N", shape)
    return shape[:200]


def line_kind(line):
    """Leading syscall name, or a line kind, for the malformed counters."""
    m = LEADING.match(line)
    if not m:
        return "other"
    return m[1] or m[2] or ("terminal" if m[3] else "signal" if m[4] else "strace")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode_c_string(token):
    """Decode exactly one quoted strace string, without accepting truncation."""
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise ValueError("malformed_string")
    out = bytearray()
    i = 1
    escapes = {"n": 10, "t": 9, "r": 13, "f": 12, "v": 11, "a": 7, "b": 8, "\\": 92, '"': 34}
    while i < len(token) - 1:
        c = token[i]
        i += 1
        if c == "\\":
            if i >= len(token) - 1:
                raise ValueError("malformed_string")
            c = token[i]
            i += 1
            if c == "x":
                h = token[i:i + 2]
                if len(h) != 2 or not re.fullmatch(r"[0-9a-fA-F]{2}", h):
                    raise ValueError("malformed_string")
                out.append(int(h, 16))
                i += 2
            elif c in "01234567":
                h = c
                while i < len(token) - 1 and len(h) < 3 and token[i] in "01234567":
                    h += token[i]
                    i += 1
                if int(h, 8) > 255:
                    raise ValueError("malformed_string")
                out.append(int(h, 8))
            elif c in escapes:
                out.append(escapes[c])
            else:
                raise ValueError("malformed_string")
        elif not 32 <= ord(c) <= 126 or c == '"':
            raise ValueError("malformed_string")
        else:
            out.append(ord(c))
    return bytes(out)


def _string(token):
    if re.search(r'"\s*\.\.\.$', token):
        raise ValueError("abbreviated_required_string")
    try:
        value = decode_c_string(token).decode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("unsupported_path_encoding") from None
    if "\x00" in value:
        raise ValueError("malformed_string")
    return value


def _split(raw):
    """Split arguments without retaining any quoted buffers in the result event."""
    items, start, depth, angle = [], 0, 0, False
    quoted = escaped = False
    quote_start = inner = 0
    for i, c in enumerate(raw):
        if quoted:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                decode_c_string(raw[quote_start:i + 1])
                quoted = False
        elif c == '"':
            quoted = True
            quote_start = i
        elif angle:
            # Socket annotations carry '->' inside brackets (<TCP:[a->b]>): only a
            # '>' outside the annotation's own brackets closes it.
            if c == "[":
                inner += 1
            elif c == "]":
                inner -= 1
            elif c == ">" and inner <= 0:
                angle = False
        elif c == "<":
            angle, inner = True, 0
        elif not angle:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "," and depth == 0:
                items.append(raw[start:i].strip())
                start = i + 1
    if quoted:
        raise ValueError("malformed_string")
    items.append(raw[start:].strip())
    return items


def _fd(raw):
    if raw == "AT_FDCWD":
        return {"fd": "AT_FDCWD", "annotation": None, "deleted": False}
    match = re.fullmatch(r"(-?\d+)(?:<(.*)>)?", raw)
    if not match:
        raise ValueError("unresolved_file_descriptor")
    annotation = match[2]
    # -xx escapes every path byte; -x escapes only non-ASCII bytes, anywhere in the path.
    if annotation and (annotation.startswith("\\x") or (annotation.startswith("/") and "\\" in annotation)):
        annotation = _string('"' + annotation + '"')
    deleted = bool(annotation and annotation.endswith(" (deleted)"))
    if deleted:
        annotation = annotation[:-10]
    return {"fd": int(match[1]), "annotation": annotation, "deleted": deleted}


def _entries(raw):
    if not raw.startswith("[") or not raw.endswith("]") or "..." in raw:
        raise ValueError("directory_enumeration_incomplete")
    rows = []
    if raw == "[]":
        return rows
    for record in _split(raw[1:-1]):
        m = re.fullmatch(r'\{d_ino=(\d+), d_off=(-?\d+), d_reclen=(\d+), d_type=(DT_[A-Z]+), d_name=("(?:\\.|[^"\\])*")\}', record)
        if not m or m[4] not in {"DT_UNKNOWN", "DT_FIFO", "DT_CHR", "DT_DIR", "DT_BLK", "DT_REG", "DT_LNK", "DT_SOCK", "DT_WHT"}:
            raise ValueError("directory_enumeration_incomplete")
        name = _string(m[5])
        if "/" in name or not name:
            raise ValueError("directory_enumeration_incomplete")
        length = int(m[3])
        if length != ((20 + len(name.encode("utf-8")) + 7) // 8) * 8:
            raise ValueError("directory_enumeration_incomplete")
        rows.append({"name": name, "reclen": length, "type": m[4], "cookie": int(m[2])})
    return rows


def _args(event, raw, resumed=False):
    args = _split(raw)
    name = event["name"]
    event.update(paths={}, fds={}, scalars={}, flags=[], entries=None, endpoints=[])
    if name == "fstat":
        size = re.search(r"\bst_size=(\d+)(?:,|\})", raw)
        if size:
            event["observed_size"] = int(size[1])
    # A resumed tail carries no trustworthy argument indices. Entry arguments
    # are already sanitized in its unfinished partner.
    if resumed:
        return
    for i in PATH_ARGS.get(name, ()):
        # utimensat(fd, NULL, ...) names the descriptor itself, not a path.
        if i < len(args) and args[i] and args[i] != "NULL":
            event["paths"][i] = _string(args[i])
    for i in FD_ARGS.get(name, ()):
        if i < len(args) and args[i]:
            event["fds"][i] = _fd(args[i])
    # Only scalar literals/flag expressions survive. Never retain unknown text,
    # argv/envp, structures, addresses, read/write buffers or socket payloads.
    for i, token in enumerate(args):
        if re.fullmatch(r"-?\d+", token):
            event["scalars"][i] = int(token)
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*(?:\|[A-Z][A-Z0-9_]*)*", token):
            event["scalars"][i] = token
            event["flags"].extend(token.split("|"))
    if name in {"ioctl", "fcntl", "prctl"}:
        # Leading identifier or number of the subtype argument, for the subtype counters only.
        index = 0 if name == "prctl" else 1
        label = re.match(r"[A-Za-z0-9_]{1,64}", args[index]) if index < len(args) else None
        event["subtype_label"] = label[0] if label else "unknown"
    if name == "clone":
        m = re.search(r"(?:^|,\s*)flags=([A-Z0-9_|]+)(?:,|$)", raw)
        if not m:
            raise ValueError("unsupported_kernel_abi")
        event["flags"] = m[1].split("|")
    if name in {"getdents", "getdents64"} and len(args) > 1:
        event["entries"] = _entries(args[1])
    if name in {"pipe", "pipe2", "socketpair"}:
        index = 3 if name == "socketpair" else 0
        if len(args) > index and args[index].startswith("["):
            event["endpoints"] = [_fd(x) for x in _split(args[index][1:-1])]
    if name in {"poll", "ppoll", "select", "pselect6"}:
        event["poll_fds"] = [int(x) for x in re.findall(r"\bfd=(\d+)", raw)]
        if name in {"select", "pselect6"}:
            event["poll_fds"] = [int(x) for a in args[1:4] if a.startswith("[") for x in re.findall(r"\d+", a)]
    if name in {"sendmsg", "recvmsg", "sendmmsg", "recvmmsg"}:
        event["rights"] = "SCM_RIGHTS" in raw
    if name in {"bind", "connect"} and len(args) > 1:
        match = re.search(r'\bsa_family=AF_UNIX,\s*sun_path=("(?:\\.|[^"\\])*")', args[1])
        if match:
            event["unix_peer"] = _string(match[1])


def _parse_line(line, sequence):
    event = dict(sequence=sequence, pid=None, time=None, kind=None, name=None,
                 errno=None, return_value=None, duration=None)
    m = re.fullmatch(r"strace: Process (\d+) (attached|detached)", line)
    if m:
        event.update(pid=int(m[1]), kind=m[2])
        return event
    # strace -f -o prints no pid until a second process exists (pid None: the root);
    # pids are left-justified in five columns; stderr output uses "[pid N] ".
    m = re.fullmatch(r"(?:\[pid +(\d+)\] |(\d+) +)?(\d+\.\d+) (.*)", line)
    if not m:
        raise ValueError("malformed_line")
    pid = m[1] or m[2]
    event.update(pid=None if pid is None else int(pid), time=m[3])
    body = m[4]
    terminal = re.fullmatch(r"\+\+\+ exited with (\d+) \+\+\+", body)
    if terminal:
        event.update(kind="exit", status=int(terminal[1]))
        return event
    terminal = re.fullmatch(r"\+\+\+ killed by (SIG[A-Z0-9]+)(?: \(core dumped\))? \+\+\+", body)
    if terminal:
        event.update(kind="killed", signal=terminal[1])
        return event
    signal = re.fullmatch(r"--- (?:stopped by )?(SIG[A-Z0-9]+)(?: \{.*\})? ---", body)
    if signal:
        event.update(kind="signal", signal=signal[1])
        return event
    start = re.match(r"([a-zA-Z_][a-zA-Z_0-9]*)\(", body)
    resumed = re.match(r"<\.\.\. ([a-zA-Z_][a-zA-Z_0-9]*) resumed>", body)
    if not start and not resumed:
        raise ValueError("malformed_line")
    match = start or resumed
    event.update(name=match[1], kind="resumed" if resumed else "syscall")
    rest = body[match.end():]
    if rest.endswith(" <unfinished ...>") and not resumed:
        event["kind"] = "unfinished"
        _args(event, rest[:-17])
        return event
    # The return annotation ends at a '>' followed by a space or end of line, never
    # at a '>' inside it (<TCP:[a->b]>, <UNIX:[1->2]>).
    returned = RETURNED.fullmatch(rest)
    if not returned:
        raise ValueError("malformed_line")
    tail = returned[4]
    if tail.startswith(" ("):
        # Auxiliary return decoding: = 1 ([{fd=5, revents=POLLIN}]), = 0x8000 (flags O_RDONLY).
        tail = tail[_balanced(tail, 1):]
    trailer = TRAILER.fullmatch(tail)
    if not trailer:
        raise ValueError("malformed_line")
    ret, errno, duration = returned[2], trailer[1], trailer[2]
    event.update(return_value=None if ret == "?" else int(ret, 16 if ret.startswith("0x") else 10),
                 errno=errno, duration=None if duration == "unavailable" else duration)
    # '?' is legal for exits, for a syscall a signal interrupted (= ? ERESTARTSYS ...),
    # and for one whose task vanished (<unfinished ...>) = ?[ <unavailable>]).
    interrupted = (errno is not None or duration == "unavailable"
                   or returned[1].rstrip().endswith("<unfinished ...>"))
    if ret == "?" and event["name"] not in {"exit", "exit_group", "rt_sigreturn"} and not interrupted:
        raise ValueError("malformed_line")
    _args(event, returned[1], resumed=bool(resumed))
    if returned[3] is not None:
        event["return_fd"] = _fd(ret + "<" + returned[3] + ">")
    return event


def _stream(data):
    # Iteration avoids a second transcript-sized splitlines allocation.
    start, sequence = 0, 0
    while start < len(data):
        end = data.find(b"\n", start)
        sequence += 1
        if end < 0:
            yield sequence, data[start:], False
            return
        yield sequence, data[start:end], True
        start = end + 1


def parse_stream(data):
    events, reasons, pending = [], [], set()
    for sequence, raw, terminated in _stream(data):
        if not terminated:
            reasons.append("trace_loss")
            continue
        try:
            event = _parse_line(raw.decode("ascii", "strict"), sequence)
            events.append(event)
            key = (event["pid"], event["name"])
            if event["kind"] == "unfinished":
                if key in pending:
                    reasons.append("unpaired_syscall")
                pending.add(key)
            elif event["kind"] == "resumed":
                if key not in pending:
                    reasons.append("unpaired_syscall")
                pending.discard(key)
            elif event["kind"] == "detached":
                reasons.append("trace_loss")
            elif event["kind"] in {"exit", "killed"}:
                # A task that exits mid-syscall never resumes it.
                pending = {k for k in pending if k[0] != event["pid"]}
        except (ValueError, UnicodeError) as exc:
            reasons.append("malformed_string" if isinstance(exc, UnicodeError) else str(exc))
    if pending:
        reasons.append("unpaired_syscall")
    return events, sorted(set(reasons))


def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _lexical(path):
    parts = []
    for p in path.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/" + "/".join(parts)


def _is_supervisor(resolution):
    return bool(resolution.external) and resolution.external.get("class") == "supervisor"


@dataclass
class Resolution:
    absolute: str = None
    path: str = None
    external: dict = None
    aliases: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def _root_class(value):
    return value if isinstance(value, str) else value.get("class") if isinstance(value, dict) else None


def _supervisor(path, context):
    """Harness-owned control roots (selection dir, scratch log) are never inputs."""
    return any(_root_class(value) == "supervisor" and _under(path, root)
               for root, value in context["external_roots"].items())


def _external(path, context):
    matches = [r for r in context["external_roots"] if _under(path, r)]
    value = context["external_roots"][max(matches, key=len)] if matches else {}
    if isinstance(value, str):
        value = {"class": value, "origin_category": value}
    if _supervisor(path, context):
        value = {"class": "supervisor", "origin_category": "supervisor"}
    row = {"class": value.get("class", "unresolved_external"),
            "origin_category": value.get("origin_category", value.get("category", "unknown")),
            "path_token": digest(path)[:16], "identity_sha256": None,
            "inventory_ref": None, "resolved": False, "reason": "identity_binding_pending"}
    identity = context.get("external_identities", {}).get(row["path_token"], {})
    row.update({k: identity[k] for k in ("identity_sha256", "inventory_ref", "resolved", "reason", "size") if k in identity})
    return row


def resolve_path(cwd, dirfd_path, raw, context, state):
    """Resolve components in order, following only inventory/witnessed symlinks."""
    result = Resolution()
    if not isinstance(raw, str) or "\x00" in raw:
        result.reasons.append("malformed_string")
        return result
    base = dirfd_path if dirfd_path is not None else cwd
    if not raw.startswith("/") and not base:
        result.reasons.append("unknown_path_base")
        return result
    source = context["source_root"].rstrip("/") or "/"
    links = context["inventory"]["symlinks"].copy()
    links.update(state.get("symlinks", {}))
    links = {(k if k.startswith("/") else source + "/" + k): v for k, v in links.items()}
    todo = (raw if raw.startswith("/") else base.rstrip("/") + "/" + raw).split("/")
    parts, count, escaped = [], 0, False
    while todo:
        part = todo.pop(0)
        if not part or part == ".":
            continue
        if part == "..":
            if "/" + "/".join(parts) == source:
                escaped = True
            if parts:
                parts.pop()
            continue
        parts.append(part)
        prefix = "/" + "/".join(parts)
        if prefix in links and not (state.get("nofollow") and not todo):
            count += 1
            if count > 40:
                result.reasons.append("symlink_loop")
                return result
            target = links[prefix]
            target_abs = target if target.startswith("/") else "/" + "/".join(parts[:-1]) + "/" + target
            normalized_target = _lexical(target_abs)
            if _under(prefix, source) and _under(normalized_target, source):
                result.aliases.append({"link": prefix[len(source):].lstrip("/"),
                                       "target": normalized_target[len(source):].lstrip("/")})
            else:
                escaped = True
            todo = target_abs.split("/") + todo
            parts = []
    result.absolute = "/" + "/".join(parts)
    if _supervisor(result.absolute, context):
        result.external = _external(result.absolute, context)
    elif _under(result.absolute, source) and not escaped:
        result.path = result.absolute[len(source):].lstrip("/") or "."
    else:
        result.external = _external(result.absolute, context)
    return result


def _validate(context):
    if not isinstance(context, dict):
        raise ValueError("invalid_context")
    for key in BINDINGS + ("capture_group", "source_root", "initial_cwd"):
        if not isinstance(context.get(key), str) or not context[key]:
            raise ValueError("invalid_context")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", context["capture_group"]):
        raise ValueError("invalid_context")
    # Absent or null root_pid: the decoder adopts the first parsed event's pid.
    if context.get("root_pid") is not None and (type(context["root_pid"]) is not int or context["root_pid"] <= 0):
        raise ValueError("invalid_context")
    if not context["source_root"].startswith("/") or not context["initial_cwd"].startswith("/"):
        raise ValueError("invalid_context")
    inventory = context.get("inventory")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("files"), (list, dict)) or not isinstance(inventory.get("symlinks"), dict):
        raise ValueError("invalid_context")
    if not isinstance(context.get("seed_fds"), dict) or not isinstance(context.get("external_roots"), dict):
        raise ValueError("invalid_context")
    for fd, row in context["seed_fds"].items():
        if not str(fd).isdigit() or not isinstance(row, dict) or row.get("kind") not in {"file", "devnull", "supervisor", "tty", "deleted"}:
            raise ValueError("invalid_context")
        if row["kind"] == "file" and not isinstance(row.get("path"), str):
            raise ValueError("invalid_context")
    for root, value in context["external_roots"].items():
        if not isinstance(root, str) or not isinstance(value, (str, dict)):
            raise ValueError("invalid_context")
    # suites_deferred: the supervisor reads the suite list when the stream ends.
    validate_suites(context.get("suites"), allow_empty=context.get("suites_deferred") is True)
    canonical(context)


def validate_suites(suites, allow_empty=False):
    if not isinstance(suites, list) or (not suites and not allow_empty):
        raise ValueError("invalid_context")
    for suite in suites:
        if (not isinstance(suite, dict) or not isinstance(suite.get("suite_id"), str) or not suite["suite_id"]
                or type(suite.get("attempt")) is not int or suite["attempt"] < 1
                or not isinstance(suite.get("worker"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", suite["worker"])
                or not isinstance(suite.get("test_ids"), list)
                or any(not isinstance(t, str) for t in suite["test_ids"])
                or not isinstance(suite.get("outcomes_ref"), str)):
            raise ValueError("invalid_context")
    return suites


class _Decoder:
    def __init__(self, context, limits):
        self.context, self.limits = context, limits
        self.reasons, self.reason_count = set(), 0
        self.stopped = False
        # Live-state estimate: rises on retain, falls on release; the cap reads measured RSS
        # where resource exists and this counter's peak only where it does not.
        self.state_bytes = self.state_peak = self.measured_peak = 0
        # Descriptor tables by identity: [table, live holders]; released when the last holder goes.
        self.tables = {}
        self.syscall_name, self.line = "other", (0, b"")
        self.trace_lost = False
        self.tasks, self.active, self.pending, self.unfinished = [], {}, {}, {}
        self.reads, self.external, self.negative, self.aliases = {}, {}, [], {}
        self.external_paths, self.written_paths, self.observed_sizes = {}, set(), {}
        self.directories, self.enumerations, self.created, self.internal = {}, [], set(), set()
        self.links = {}
        self.supervisor_reads = 0
        self.unix_peers, self.unix_sockets = {}, []
        self.event_count = self.syscall_count = 0
        self.loss = dict(unpaired=0, malformed=0, unknown_syscalls=0, oversize=0, unknown_descriptors=0)
        # Calibration instrument: sanitized shapes and bounded name counters.
        self.malformed_samples, self.malformed_by_name, self.unsupported_by_name = {}, {}, {}
        self.unpaired_samples, self.unpaired_by_name, self.unknown_fd_by_name = {}, {}, {}
        self.subtypes = {"prctl": {}, "fcntl": {}}
        self.root = None
        # Under strace the tracee pid is known only from the stream; strace -f
        # always opens with the tracee's execve, so the first event names it.
        self.adopt_root = context.get("root_pid") is None
        if not self.adopt_root:
            self.birth_root(context["root_pid"])

    def birth_root(self, pid):
        self.adopt_root = False
        self.root = self.birth(pid, 0, None, [])
        if self.root is not None:
            for fd, seed in self.context["seed_fds"].items():
                if self.stopped:
                    break
                desc = dict(seed)
                if desc["kind"] == "file":
                    path = desc["path"]
                    if not path.startswith("/"):
                        path = self.context["source_root"] + "/" + path
                    desc["resolution"] = resolve_path(None, None, path, self.context, {})
                    desc["path"] = desc["resolution"].absolute
                self.putfd(self.root, int(fd), desc, bool(seed.get("cloexec")))

    @staticmethod
    def count(table, key):
        if key in table or len(table) < NAME_LIMIT:
            table[key] = table.get(key, 0) + 1
        else:
            table["(other)"] = table.get("(other)", 0) + 1

    @staticmethod
    def sample(table, raw):
        """Count a line's sanitized shape; at most SAMPLE_LIMIT distinct shapes are kept."""
        shape = malformed_shape(raw[:SAMPLE_PREFIX].decode("ascii", "replace"))
        if shape in table or len(table) < SAMPLE_LIMIT:
            table[shape] = table.get(shape, 0) + 1

    def malformed(self, raw, reason):
        """Count an undecodable line by leading name and sanitized shape; never keep the line."""
        text = raw.decode("ascii", "replace")
        self.count(self.malformed_by_name, line_kind(text))
        self.sample(self.malformed_samples, raw)
        self.reason(reason)

    def unpaired(self, name, raw):
        self.count(self.unpaired_by_name, name)
        self.sample(self.unpaired_samples, raw)
        self.reason("unpaired_syscall")

    def claims_root(self, event, pid):
        """Is an unknown prefixed pid the unprefixed root rather than an unborn child?

        It is the root when it resumes a syscall the root left unfinished, or when no
        traced task has a fork in flight (no unborn child can exist). Otherwise the
        event waits as pending; an unresolved wait ends as unexplained_pid.
        """
        if pid in self.pending:
            return False
        if event["kind"] == "resumed" and (None, event["name"]) in self.unfinished:
            return True
        return not any(name in {"clone", "fork", "vfork"} for _, name in self.unfinished)

    def name_root(self, pid):
        record = self.root["record"]
        record["pid"] = pid
        self.active[pid] = self.active.pop(None)
        for key in [key for key in self.unfinished if key[0] is None]:
            self.unfinished[(pid, key[1])] = self.unfinished.pop(key)

    def reason(self, value):
        if value == "trace_loss":
            self.trace_lost = True
        if value == "unpaired_syscall":
            self.loss["unpaired"] += 1
        elif value.startswith("malformed"):
            self.loss["malformed"] += 1
        elif value.startswith("unsupported_syscall"):
            self.loss["unknown_syscalls"] += 1
            self.count(self.unsupported_by_name, value[len("unsupported_syscall:"):])
        elif value == "unresolved_file_descriptor":
            self.loss["unknown_descriptors"] += 1
            self.count(self.unknown_fd_by_name, self.syscall_name)
        if value in self.reasons:
            return
        self.reason_count += 1
        if len(self.reasons) < self.limits["max_reasons"]:
            self.reasons.add(value)
        elif not self.stopped:
            self.reasons.remove(max(self.reasons))
            self.reasons.add("capture_limit_exceeded:max_reasons")
            self.stopped = True
            self.reads.clear()
            self.external.clear()
            self.negative.clear()
            self.aliases.clear()
            self.directories.clear()

    def cap(self, name):
        self.reason("capture_limit_exceeded:" + name)
        self.stopped = True
        # Discard the entire dependency payload, never a seemingly complete prefix.
        self.reads.clear()
        self.external.clear()
        self.negative.clear()
        self.aliases.clear()
        self.directories.clear()

    def retain(self, amount):
        self.state_bytes += amount
        if self.state_bytes > self.state_peak:
            self.state_peak = self.state_bytes
            if resource is None and self.state_bytes > self.limits["max_state_bytes"] and not self.stopped:
                self.cap("max_state_bytes")

    def release(self, amount):
        self.state_bytes = max(0, self.state_bytes - amount)

    def measure(self):
        """Live cap: this process's measured peak RSS against max_state_bytes (no-op without resource)."""
        if resource is None:
            return
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * RSS_SCALE
        self.measured_peak = max(self.measured_peak, peak)
        if peak > self.limits["max_state_bytes"] and not self.stopped:
            self.cap("max_state_bytes")

    @property
    def peak_bytes(self):
        return self.measured_peak if resource is not None else self.state_peak

    def hold(self, table):
        entry = self.tables.get(id(table))
        if entry is None:
            self.tables[id(table)] = [table, 1]
        else:
            entry[1] += 1

    def drop(self, table):
        """One holder of a descriptor table is gone; True when it was the last and the table is released."""
        entry = self.tables.get(id(table))
        if entry is None:
            return False
        entry[1] -= 1
        if entry[1] > 0:
            return False
        del self.tables[id(table)]
        self.release(128 * len(table))
        return True

    def birth(self, pid, sequence, parent, flags):
        if len(self.tasks) >= self.limits["max_tasks"]:
            self.cap("max_tasks")
            return None
        if pid in self.active and self.active[pid]["record"]["exit"] is None:
            self.reason("unexplained_pid")
            return None
        shares = [s for s in ("CLONE_FS", "CLONE_FILES") if s in flags]
        record = dict(pid=pid, birth_sequence=sequence,
                      parent=None if parent is None else {k: parent["record"][k] for k in ("pid", "birth_sequence")},
                      shares=shares, exec_count=0, exit=None)
        if parent is None:
            cwd, fds = {"path": self.context["initial_cwd"]}, {}
        else:
            cwd = parent["cwd"] if "CLONE_FS" in flags else dict(parent["cwd"])
            fds = parent["fds"] if "CLONE_FILES" in flags else {k: dict(v) for k, v in parent["fds"].items()}
        task = dict(record=record, cwd=cwd, fds=fds, last=None)
        self.tasks.append(task)
        self.active[pid] = task
        self.hold(fds)
        self.retain(256 + (0 if parent is not None and "CLONE_FILES" in flags else 128 * len(fds)))
        return task

    def putfd(self, task, fd, desc, cloexec=False):
        if self.stopped:
            return
        if fd not in task["fds"]:
            if len(task["fds"]) >= self.limits["max_fds_per_task"]:
                self.cap("max_fds_per_task")
                return
            self.retain(128)
        task["fds"][fd] = {"description": desc, "cloexec": cloexec}

    def closefd(self, task, fd):
        if task["fds"].pop(fd, None) is not None:
            self.release(128)

    def fd(self, task, argument):
        if argument is None or argument["fd"] not in task["fds"]:
            self.reason("unresolved_file_descriptor")
            return None
        desc = task["fds"][argument["fd"]]["description"]
        if desc["kind"] == "deleted":
            self.reason("unresolved_file_descriptor")
            return None
        annotation = argument.get("annotation")
        if annotation:
            if desc["kind"] == "file":
                resolution = resolve_path(task["cwd"]["path"], None, annotation, self.context, {"symlinks": self.links})
                if resolution.absolute != desc.get("path"):
                    self.reason("descriptor_annotation_mismatch")
                if argument.get("deleted"):
                    desc["deleted"] = True
            elif desc["kind"] == "endpoint":
                if not re.match(r"(?:pipe:|socket:|UNIX:|TCP:|anon_inode:)", annotation):
                    self.reason("descriptor_annotation_mismatch")
        return desc

    def resolve(self, task, event, index, nofollow=False):
        raw = event["paths"].get(index)
        if raw is None:
            self.reason("malformed_string")
            return Resolution()
        base = None
        if not raw.startswith("/") and index > 0 and index - 1 in event["fds"]:
            arg = event["fds"][index - 1]
            if arg["fd"] != "AT_FDCWD":
                desc = self.fd(task, arg)
                base = desc.get("path") if desc else None
                if base is None:
                    self.reason("unknown_path_base")
                    return Resolution()
        if raw == "" and "AT_EMPTY_PATH" not in event["flags"]:
            self.reason("unknown_path_base")
            return Resolution()
        resolution = resolve_path(event.get("entry_cwd", task["cwd"]["path"]), base, raw, self.context,
                                  {"symlinks": self.links, "nofollow": nofollow})
        for reason in resolution.reasons:
            self.reason(reason)
        for alias in resolution.aliases:
            self.aliases[canonical(alias)] = alias
        return resolution

    def edge(self, resolution, kind, event, allow_missing=False):
        if not resolution.absolute or self.stopped:
            return
        if _is_supervisor(resolution):
            # Harness control files: counted, never a dependency or lookup.
            self.supervisor_reads += 1
            return
        error = event.get("errno")
        added = False
        if error in {"ENOENT", "ENOTDIR"}:
            self.negative.append({"path_or_class": resolution.path if resolution.path is not None else resolution.external["class"],
                                  "scope": "repo" if resolution.path is not None else "external",
                                  "errno": error, "syscall": event["name"], "sequence": event["sequence"]})
            added = True
        elif resolution.absolute in self.created:
            token = digest(resolution.absolute)[:16]
            added = token not in self.internal
            self.internal.add(token)
        elif resolution.path is not None:
            files = self.context["inventory"]["files"]
            symlinks = self.context["inventory"]["symlinks"]
            if resolution.path not in files and resolution.absolute not in files and resolution.path not in symlinks and resolution.absolute not in symlinks and not allow_missing:
                self.reason("unlisted_repo_path")
            row = {"path": resolution.path, "kind": kind}
            if error:
                row["errno"] = error
            key = canonical(row)
            added = key not in self.reads
            self.reads[key] = row
        elif resolution.external:
            row = resolution.external
            added = (row["class"], row["path_token"]) not in self.external
            self.external[(row["class"], row["path_token"])] = row
            if row["path_token"] not in self.external_paths:
                self.external_paths[row["path_token"]] = {"path": resolution.absolute, "class": row["class"]}
                self.retain(len(resolution.absolute.encode("utf-8")) + 128)
        if len(self.reads) + len(self.external) + len(self.negative) + len(self.directories) > self.limits["max_dependencies"]:
            self.cap("max_dependencies")
        if added:
            self.retain(160)

    def written(self, path):
        if path not in self.written_paths:
            self.written_paths.add(path)
            self.retain(64)

    def mutate(self, resolution):
        if not resolution.absolute:
            return
        parent = resolution.absolute.rsplit("/", 1)[0] or "/"
        for enumeration in self.enumerations:
            if not enumeration["eof"] and enumeration["path"] == parent:
                enumeration["invalid"] = True
                self.reason("directory_enumeration_incomplete")

    def enumerate(self, desc, event):
        if not desc or desc["kind"] != "file" or not desc.get("directory"):
            self.reason("directory_enumeration_incomplete")
            return
        enumeration = desc["enumeration"]
        enumeration["started"] = True
        ret, entries = event["return_value"], event.get("entries")
        if ret is None or ret < 0 or entries is None or sum(e["reclen"] for e in entries) != ret or enumeration["eof"]:
            self.reason("directory_enumeration_incomplete")
            enumeration["invalid"] = True
            return
        for entry in entries:
            name = entry["name"]
            if name not in {".", ".."}:
                if name not in enumeration["members"]:
                    self.retain(64 + len(name.encode("utf-8")))
                enumeration["members"].add(name)
        if ret == 0:
            enumeration["eof"] = True
            path = desc["resolution"].path
            if not enumeration["invalid"] and path is not None:
                members = sorted(("" if path == "." else path + "/") + n for n in enumeration["members"])
                if path in self.directories and self.directories[path] != members:
                    self.reason("directory_membership_conflict")
                else:
                    self.directories[path] = members
                if sum(len(m) for m in self.directories.values()) + len(self.reads) + len(self.external) + len(self.negative) > self.limits["max_dependencies"]:
                    self.cap("max_dependencies")
            elif path is None:
                self.edge(desc["resolution"], "stat", event)

    def policy(self, event, task):
        name, flags = event["name"], event["flags"]
        if name in POLICY["namespace"] or any(f.startswith("CLONE_NEW") for f in flags):
            self.reason("namespace_change")
        if name in POLICY["abi"]:
            if event["errno"] != "ENOSYS":
                self.reason("unsupported_kernel_abi")
            return False
        # syscall_0xNN / syscall_NNN (unnamed by strace) are never known.
        if name in POLICY["reject"] or name not in KNOWN:
            self.reason("unsupported_syscall:" + name)
            return False
        if name in {"ioctl", "fcntl", "prctl"}:
            subtype = event["scalars"].get(0 if name == "prctl" else 1, "unknown")
            if subtype not in POLICY[name]:
                if name in self.subtypes:
                    # The reason keeps the bounded ':unknown' form; the counter keeps the name.
                    self.count(self.subtypes[name], event.get("subtype_label", "unknown"))
                self.reason("unsupported_syscall:" + name + ":" + str(subtype))
                return False
        if name in {"open", "openat"}:
            index = 2 if name == "openat" else 1
            value = event["scalars"].get(index)
            if isinstance(value, str):
                if any(flag not in POLICY["open_flags"] for flag in value.split("|")):
                    self.reason("unsupported_kernel_abi")
            elif value not in {0, 1, 2}:
                self.reason("unsupported_kernel_abi")
        if name == "clone" and any(flag not in POLICY["clone_flags"] and not flag.startswith("CLONE_NEW") for flag in flags):
            self.reason("unsupported_kernel_abi")
        # Amendment 4 withdrew the shared-futex rule: a futex is not a file input.
        for fd in event.get("poll_fds", []):
            self.fd(task, {"fd": fd})
        if name in POLICY["network"]:
            if event.get("rights"):
                self.reason("unsupported_syscall:" + name + ":SCM_RIGHTS")
        return True

    def filesystem_events(self):
        """inotify is an external input the capture cannot bind: an unresolved marker row."""
        row = _external("filesystem-events", {"external_roots": {}})
        row.update({"class": "filesystem-events", "origin_category": "filesystem-events"})
        self.external[("filesystem-events", row["path_token"])] = row

    def metadata_mutation(self, task, event):
        """chmod/chown/utimensat families write metadata: the target counts as written."""
        name, flags = event["name"], event["flags"]
        index = PATH_ARGS.get(name, (None,))[0]
        if index is not None and index in event["paths"]:
            nofollow = name == "lchown" or "AT_SYMLINK_NOFOLLOW" in flags
            path = self.resolve(task, event, index, nofollow=nofollow).absolute
        else:
            # fchmod/fchown, and utimensat(fd, NULL, ...), act on the descriptor.
            argument = event["fds"].get(0)
            if argument is None or argument["fd"] == "AT_FDCWD":
                self.reason("unknown_path_base")
                return
            desc = self.fd(task, argument)
            path = desc.get("path") if desc else None
        if path and event["return_value"] is not None and event["return_value"] >= 0:
            self.written(path)

    def network_input(self):
        row = _external("network", {"external_roots": {}})
        row.update({"class": "network", "origin_category": "network"})
        self.external[("network", row["path_token"])] = row

    def network(self, task, event):
        name, ret = event["name"], event["return_value"]
        success = ret is not None and ret >= 0
        if name == "socket":
            if success:
                desc = {"kind": "network", "unix": "AF_UNIX" in event["flags"], "verified_peer": False}
                self.putfd(task, ret, desc, "SOCK_CLOEXEC" in event["flags"])
                if desc["unix"]:
                    self.unix_sockets.append(desc)
                    return
            self.network_input()
            return
        if name == "setsockopt":
            # Socket configuration is a no-op for inputs; the descriptor must still resolve.
            if 0 in event["fds"]:
                self.fd(task, event["fds"][0])
            return
        entry = task["fds"].get(event["fds"].get(0, {}).get("fd"))
        desc = entry["description"] if entry else None
        peer = event.get("unix_peer")
        if desc and desc.get("unix") and peer is not None:
            resolved = resolve_path(task["cwd"]["path"], None, peer, self.context, {})
            if name == "bind" and success and resolved.absolute:
                self.unix_peers[resolved.absolute] = desc
                return
            target = self.unix_peers.get(resolved.absolute)
            if name == "connect" and success and target:
                desc.update(kind="endpoint", verified_peer=True)
                target.update(kind="endpoint", verified_peer=True)
                return
        if desc and desc["kind"] == "endpoint":
            if name in {"accept", "accept4"} and success:
                self.putfd(task, ret, {"kind": "endpoint"}, "SOCK_CLOEXEC" in event["flags"])
            return
        # Listening on a newly bound UNIX endpoint is provisional until its
        # in-tree peer is witnessed. A socket with no such peer rejects closure.
        if desc and desc.get("unix") and name == "listen":
            return
        self.network_input()
        if name in {"accept", "accept4"} and success:
            self.putfd(task, ret, {"kind": "network"}, "SOCK_CLOEXEC" in event["flags"])

    def copy(self, task, event):
        """sendfile/copy_file_range: a read edge for the in-descriptor's path, a write for the out's."""
        ret = event["return_value"]
        source, sink = (event["fds"].get(i) for i in COPIES[event["name"]])
        if source is None or sink is None:
            self.reason("unresolved_file_descriptor")
            return
        into, out = self.fd(task, source), self.fd(task, sink)
        if into is None or out is None or ret is None or ret < 0:
            return
        if into["kind"] == "file":
            self.edge(into["resolution"], "open", event)
        if out["kind"] == "file":
            out["written"] = True
            self.written(out["path"])

    def syscall(self, task, event):
        name, ret, flags = event["name"], event["return_value"], event["flags"]
        success = ret is not None and ret >= 0
        self.syscall_name = name
        if not self.policy(event, task):
            return
        if name in {"fork", "vfork", "clone"} and success and ret > 0:
            child = self.birth(ret, event["sequence"], task, flags)
            if child:
                for queued, amount in self.pending.pop(ret, []):
                    self.release(amount)
                    self.process(queued)
            return
        if name in COPIES:
            self.copy(task, event)
            return
        if name in POLICY["network"]:
            self.network(task, event)
            return
        if name in POLICY["internal_object"]:
            if success:
                if name == "memfd_create":
                    self.internal.add(digest([event["pid"], event["sequence"], ret])[:16])
                    self.retain(64)
                self.putfd(task, ret, {"kind": "internal" if name == "memfd_create" else "endpoint"},
                           "MFD_CLOEXEC" in flags or "TFD_CLOEXEC" in flags)
            return
        if name in POLICY["filesystem_events"]:
            if name.startswith("inotify_init") and success:
                self.putfd(task, ret, {"kind": "endpoint"}, "IN_CLOEXEC" in flags)
            self.filesystem_events()
            return
        if name in METADATA_MUTATIONS:
            self.metadata_mutation(task, event)
            return
        if name in {"open", "openat", "creat"}:
            if success and "O_TMPFILE" in flags:
                token = digest([event["pid"], event["sequence"], ret])[:16]
                self.internal.add(token)
                self.putfd(task, ret, {"kind": "internal"}, "O_CLOEXEC" in flags)
                return
            index = 1 if name == "openat" else 0
            resolution = self.resolve(task, event, index, nofollow="O_NOFOLLOW" in flags)
            if success and ("O_TRUNC" in flags or name == "creat"):
                self.written(resolution.absolute)
            if "O_CREAT" in flags or name == "creat":
                self.mutate(resolution)
                if success:
                    if name == "creat" or "O_EXCL" in flags or "O_TRUNC" in flags:
                        self.created.add(resolution.absolute)
                    elif (resolution.path not in self.context["inventory"]["files"] and resolution.absolute not in self.created
                          and not _is_supervisor(resolution)):
                        self.reason("ambiguous_creation")
            if not success:
                self.edge(resolution, "open", event)
            else:
                desc = dict(kind="file", path=resolution.absolute, resolution=resolution, flags=flags,
                            deleted=False, written=False, directory="O_DIRECTORY" in flags)
                if desc["directory"]:
                    enumeration = dict(path=resolution.absolute, members=set(), eof=False, started=False, invalid=False)
                    desc["enumeration"] = enumeration
                    self.enumerations.append(enumeration)
                self.putfd(task, ret, desc, "O_CLOEXEC" in flags)
                if "return_fd" in event:
                    self.fd(task, event["return_fd"])
                if not desc["directory"]:
                    self.edge(resolution, "open", event)
            return
        if name in {"execve", "execveat"}:
            resolution = self.resolve(task, event, 1 if name == "execveat" else 0)
            self.edge(resolution, "exec", event)
            if success:
                task["record"]["exec_count"] += 1
                # Exec unshares the descriptor table, then closes its CLOEXEC fds.
                table = {fd: dict(v) for fd, v in task["fds"].items() if not v["cloexec"]}
                self.drop(task["fds"])
                task["fds"] = table
                self.hold(table)
                self.retain(128 * len(table))
            return
        if name in LOOKUPS:
            index = 1 if name in {"newfstatat", "statx", "faccessat", "readlinkat"} else 0
            nofollow = name in {"readlink", "readlinkat", "lstat", "lgetxattr", "llistxattr"} or "AT_SYMLINK_NOFOLLOW" in flags
            resolution = self.resolve(task, event, index, nofollow=nofollow)
            self.edge(resolution, "readlink" if name.startswith("readlink") else "stat", event)
            if success and name.startswith("readlink"):
                followed = self.resolve(task, event, index)
                for alias in followed.aliases:
                    self.aliases[canonical(alias)] = alias
            if name == "statfs":
                row = _external("filesystem-capacity", {"external_roots": {}})
                self.external[(row["class"], row["path_token"])] = row
            return
        if name == "chdir" and success:
            resolution = self.resolve(task, event, 0)
            task["cwd"]["path"] = resolution.absolute
            return
        if name in MUTATIONS or name == "truncate":
            resolutions = [self.resolve(task, event, i, nofollow=True) for i in PATH_ARGS.get(name, ())]
            for resolution in resolutions:
                self.mutate(resolution)
                if success:
                    self.written(resolution.absolute)
                if resolution.path in self.context["inventory"]["symlinks"] or resolution.absolute in self.links:
                    self.reason("unsupported_alias_race")
            if success and resolutions:
                if name.startswith(("mkdir", "rename", "link", "symlink")):
                    target = resolutions[-1]
                    self.created.add(target.absolute)
                    if name.startswith("symlink"):
                        self.links[target.absolute] = event["paths"][0]
                    if name.startswith("link"):
                        origin = resolutions[0]
                        files = self.context["inventory"]["files"]
                        if origin.absolute in self.created:
                            pass
                        elif origin.path is not None and (origin.path in files or origin.absolute in files):
                            # The new name carries the inventory file's bytes: the origin is read.
                            self.edge(origin, "open", event)
                        else:
                            self.reason("unsupported_syscall:" + name + ":unwitnessed_origin")
                if name.startswith(("unlink", "rename")):
                    for owner in self.tasks:
                        for entry in owner["fds"].values():
                            if entry["description"].get("path") == resolutions[0].absolute:
                                entry["description"]["deleted"] = True
            return
        if name in {"pipe", "pipe2", "socketpair"} and success:
            if len(event["endpoints"]) != 2:
                self.reason("unresolved_file_descriptor")
            for endpoint in event["endpoints"]:
                self.putfd(task, endpoint["fd"], {"kind": "endpoint"}, "O_CLOEXEC" in flags or "SOCK_CLOEXEC" in flags)
            return
        if name in {"epoll_create", "epoll_create1", "eventfd", "eventfd2"} and success:
            self.putfd(task, ret, {"kind": "endpoint"}, "EPOLL_CLOEXEC" in flags or "EFD_CLOEXEC" in flags)
            return
        if name == "mmap" and "MAP_ANONYMOUS" in flags:
            if "MAP_SHARED" in flags:
                self.reason("unsupported_syscall:mmap:shared_anonymous")
            return
        if not event["fds"]:
            return
        argument = event["fds"].get(4 if name == "mmap" else 0)
        if argument is None or argument["fd"] == "AT_FDCWD":
            return
        desc = self.fd(task, argument)
        if desc is None:
            return
        fd = argument["fd"]
        if name in READS | FD_LOOKUPS | {"mmap"} and success:
            if desc["kind"] == "file":
                if name == "fstat" and "observed_size" in event and desc["path"] not in self.observed_sizes:
                    self.observed_sizes[desc["path"]] = event["observed_size"]
                    self.retain(64)
                if name == "mmap" and "MAP_SHARED" in flags and "PROT_WRITE" in flags:
                    self.written(desc["path"])
                self.edge(desc["resolution"], "mmap" if name == "mmap" else "stat" if name in FD_LOOKUPS else "open", event)
            elif desc["kind"] not in {"devnull", "supervisor", "endpoint", "internal"}:
                self.reason("unresolved_file_descriptor")
        elif name in WRITES and success:
            desc["written"] = True
            if desc.get("path"):
                self.written(desc["path"])
        elif name == "fchdir" and success:
            if desc["kind"] != "file" or not desc.get("directory"):
                self.reason("unknown_path_base")
            else:
                task["cwd"]["path"] = desc["path"]
        elif name in {"dup", "dup2", "dup3"} and success:
            destination = event["fds"].get(1)
            if destination and destination["fd"] in task["fds"]:
                self.fd(task, destination)
            self.putfd(task, ret, desc, name == "dup3" and "O_CLOEXEC" in flags)
            if "return_fd" in event:
                self.fd(task, event["return_fd"])
        elif name == "fcntl" and success:
            command = event["scalars"].get(1)
            if command in {"F_DUPFD", "F_DUPFD_CLOEXEC"}:
                self.putfd(task, ret, desc, command == "F_DUPFD_CLOEXEC")
                if "return_fd" in event:
                    self.fd(task, event["return_fd"])
            elif command == "F_SETFD":
                task["fds"][fd]["cloexec"] = "FD_CLOEXEC" in flags or event["scalars"].get(2) == 1
        elif name == "ioctl" and success and event["scalars"].get(1) in {"FIOCLEX", "FIONCLEX"}:
            task["fds"][fd]["cloexec"] = event["scalars"][1] == "FIOCLEX"
        elif name in {"getdents", "getdents64"}:
            self.enumerate(desc, event)
        elif name == "lseek" and desc.get("directory") and not desc["enumeration"]["eof"]:
            desc["enumeration"]["invalid"] = True
            self.reason("directory_enumeration_incomplete")
        elif name == "close" and success:
            if desc.get("directory") and desc["enumeration"]["started"] and not desc["enumeration"]["eof"]:
                self.reason("directory_enumeration_incomplete")
            self.closefd(task, fd)

    def process(self, event):
        if self.stopped:
            return
        kind, pid = event["kind"], event["pid"]
        if pid is None:
            # No pid prefix: strace has traced one process so far, so the line is the
            # root's. The first such line births a root whose pid is learned later.
            if self.adopt_root:
                self.birth_root(None)
            if self.root is None:
                if not self.stopped:
                    self.reason("unexplained_pid")
                return
            pid = event["pid"] = self.root["record"]["pid"]
        elif self.adopt_root:
            self.birth_root(pid)
        elif self.root is not None and self.root["record"]["pid"] is None and pid not in self.active:
            if kind != "attached" and self.claims_root(event, pid):
                self.name_root(pid)
        if self.stopped:
            return
        if kind == "attached":
            return
        if kind == "detached":
            self.reason("trace_loss")
            return
        task = self.active.get(pid)
        if task is None or task["record"]["exit"] is not None:
            amount = len(canonical(event))
            self.pending.setdefault(pid, []).append((event, amount))
            self.retain(amount)
            return
        raw = self.raw_of(event)
        task["last"] = (event["sequence"], raw)
        if kind in {"exit", "killed"}:
            # A syscall still unfinished when its task exits never resumes: not unpaired.
            for key in [key for key in self.unfinished if key[0] == pid]:
                self.release(self.unfinished.pop(key)[5])
            task["record"]["exit"] = dict(kind=kind, sequence=event["sequence"])
            task["record"]["exit"].update({"status": event["status"]} if kind == "exit" else {"signal": event["signal"]})
            # A descendant killed by a signal is an ordinary exit record; only the root's is fatal.
            if kind == "killed" and task is self.root:
                self.reason("root_killed")
            if self.drop(task["fds"]):
                task["fds"] = {}
            return
        if kind == "signal":
            return
        key = (pid, event["name"])
        if kind == "unfinished":
            if key in self.unfinished:
                self.release(self.unfinished[key][5])
                self.unpaired(event["name"], self.unfinished[key][4])
            # Capture the entry cwd and the descriptions of the descriptors this call uses only.
            snapshot = {fd: (task["fds"].get(fd) or {}).get("description") for fd in self.used_fds(event)}
            amount = 256 + len(raw) + 64 * len(snapshot)
            self.unfinished[key] = (event, task["cwd"]["path"], snapshot, task, raw, amount)
            self.retain(amount)
            return
        if kind == "resumed":
            pending = self.unfinished.pop(key, None)
            if pending is None:
                self.unpaired(event["name"], raw)
                return
            original, cwd, snapshot, owner, _, amount = pending
            self.release(amount)
            joined = dict(original)
            for field_name in ("return_value", "return_fd", "errno", "duration", "observed_size"):
                if field_name in event:
                    joined[field_name] = event[field_name]
            joined["kind"] = "syscall"
            joined["entry_cwd"] = cwd
            # Sibling threads may change unrelated table rows; only this call's own inputs matter.
            relative = any(not p.startswith("/") for p in original["paths"].values())
            if ((relative and cwd != owner["cwd"]["path"])
                    or any((owner["fds"].get(fd) or {}).get("description") is not desc for fd, desc in snapshot.items())):
                self.reason("ambiguous_shared_state")
            self.syscall(owner, joined)
            return
        self.syscall(task, event)

    def raw_of(self, event):
        """Bounded raw prefix of the event's own line; empty for a replayed pending event."""
        sequence, raw = self.line
        return raw[:SAMPLE_PREFIX] if sequence == event["sequence"] else b""

    @staticmethod
    def used_fds(event):
        used = [a["fd"] for a in event["fds"].values() if a["fd"] != "AT_FDCWD"]
        return used + [fd for fd in event.get("poll_fds", ()) if fd not in used]


def decode(data, context, limits=None):
    return decode_stream((data,), context, limits)


def _chunk_lines(chunks, decoder, counters):
    partial = bytearray()
    sequence, length = 0, 0
    for chunk in chunks:
        counters["hash"].update(chunk)
        counters["byte_count"] += len(chunk)
        if chunk:
            counters["terminated"] = chunk.endswith(b"\n")
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end
            length += stop - start
            if length <= decoder.limits["max_record_bytes"]:
                partial.extend(chunk[start:stop])
            else:
                partial.clear()
            if end < 0:
                break
            sequence += 1
            if length > decoder.limits["max_record_bytes"]:
                decoder.loss["oversize"] += 1
                decoder.cap("max_record_bytes")
            yield sequence, bytes(partial), True
            partial.clear()
            length = 0
            start = end + 1
    if length:
        if length > decoder.limits["max_record_bytes"]:
            decoder.loss["oversize"] += 1
            decoder.cap("max_record_bytes")
        yield sequence + 1, bytes(partial), False


def _ranked(table):
    return sorted(table.items(), key=lambda row: (-row[1], row[0]))


def decode_stream(chunks, context, limits=None):
    _validate(context)
    caps = dict(LIMITS)
    for key, value in (limits or {}).items():
        if key not in caps or type(value) is not int or value < 1:
            raise ValueError("invalid_limit")
        caps[key] = value
    decoder = _Decoder(context, caps)
    counters = {"hash": hashlib.sha256(), "byte_count": 0, "terminated": False}
    for sequence, raw, terminated in _chunk_lines(chunks, decoder, counters):
        decoder.event_count = sequence
        if sequence % RSS_INTERVAL == 1:
            decoder.measure()
        if not terminated:
            decoder.reason("trace_loss")
            continue
        if len(raw) > caps["max_record_bytes"]:
            decoder.loss["oversize"] += 1
            decoder.cap("max_record_bytes")
        if decoder.stopped:
            continue
        decoder.line = (sequence, raw)
        try:
            event = _parse_line(raw.decode("ascii", "strict"), sequence)
            if event["kind"] in {"syscall", "unfinished"}:
                decoder.syscall_count += 1
            decoder.process(event)
        except (ValueError, UnicodeError) as exc:
            reason = "malformed_string" if isinstance(exc, UnicodeError) else str(exc)
            if reason.startswith("malformed"):
                decoder.malformed(raw, reason)
            else:
                decoder.syscall_name = line_kind(raw[:SAMPLE_PREFIX].decode("ascii", "replace"))
                decoder.reason(reason)
    decoder.measure()
    for (_, name), pending in list(decoder.unfinished.items()):
        decoder.unpaired(name, pending[4])
    if decoder.pending:
        decoder.reason("unexplained_pid")
    if decoder.root is None and not decoder.stopped:
        decoder.reason("missing_terminal")
    missing_exits = []
    for task in decoder.tasks:
        if task["record"]["exit"] is None:
            decoder.reason("missing_terminal" if task is decoder.root else "descendant_outlived_tree")
            if len(missing_exits) < SAMPLE_LIMIT:
                record, last = task["record"], task["last"]
                missing_exits.append({"pid": record["pid"], "birth_sequence": record["birth_sequence"],
                                      "parent_pid": record["parent"]["pid"] if record["parent"] else None,
                                      "last_sequence": last[0] if last else None,
                                      "last_shape": malformed_shape(last[1].decode("ascii", "replace")) if last else None})
    for enumeration in decoder.enumerations:
        if enumeration["started"] and not enumeration["eof"]:
            decoder.reason("directory_enumeration_incomplete")
    if not decoder.stopped and any(not desc["verified_peer"] for desc in decoder.unix_sockets):
        decoder.network_input()
    sha = counters["hash"].hexdigest()
    receipt = context.get("terminal_receipt", {})
    matched = isinstance(receipt, dict) and receipt.get("byte_count") == counters["byte_count"] and receipt.get("sha256") == sha
    if not matched or not counters["terminated"]:
        decoder.reason("trace_loss")
    if decoder.stopped:
        decoder.reads.clear()
        decoder.external.clear()
        decoder.negative.clear()
        decoder.aliases.clear()
        decoder.directories.clear()
    reasons = sorted(decoder.reasons)
    records = [task["record"] for task in decoder.tasks]
    exits = sum(record["exit"] is not None for record in records)
    children = all(task["record"]["exit"] is not None for task in decoder.tasks if task is not decoder.root) and not decoder.pending and not decoder.stopped
    complete = decoder.reason_count == 0 and matched and exits == len(records) and decoder.root is not None
    seeds = {}
    for fd, seed in context["seed_fds"].items():
        row = {"kind": seed["kind"]}
        if seed["kind"] == "file":
            path = seed["path"]
            resolution = resolve_path(context["source_root"], None, path, context, {})
            if resolution.path is not None:
                row["path"] = resolution.path
        seeds[str(fd)] = row
    cwd = resolve_path(None, None, context["initial_cwd"], context, {})
    certificate = {key: context[key] for key in BINDINGS}
    certificate.update(schema="leaf.ci.process-tree.v1", capture_group=context["capture_group"],
                       root={"pid": decoder.root["record"]["pid"] if decoder.root is not None else context.get("root_pid"),
                             "birth_sequence": 0},
                       initial_cwd=cwd.path if cwd.path is not None else {"class": cwd.external["class"]},
                       seed_fds=seeds, tasks=records, expected_exits=len(records), observed_exits=exits,
                       event_count=decoder.event_count, syscall_count=decoder.syscall_count,
                       transcript_bytes=counters["byte_count"], transcript_sha256=sha, receipt_matched=matched,
                       capture_epoch=context.get("capture_epoch"), capture_epoch_manifest=context.get("capture_epoch_manifest"),
                       syscall_policy_digest=digest(POLICY), loss_counters=decoder.loss,
                       malformed_samples=[{"shape": shape, "count": n} for shape, n in _ranked(decoder.malformed_samples)],
                       malformed_by_name=dict(_ranked(decoder.malformed_by_name)[:NAME_REPORT]),
                       unsupported_by_name=dict(_ranked(decoder.unsupported_by_name)),
                       unpaired_samples=[{"shape": shape, "count": n} for shape, n in _ranked(decoder.unpaired_samples)],
                       unpaired_by_name=dict(_ranked(decoder.unpaired_by_name)[:NAME_REPORT]),
                       unknown_fd_by_name=dict(_ranked(decoder.unknown_fd_by_name)[:NAME_REPORT]),
                       missing_exit_samples=missing_exits,
                       prctl_subtypes=dict(_ranked(decoder.subtypes["prctl"])[:NAME_REPORT]),
                       fcntl_subtypes=dict(_ranked(decoder.subtypes["fcntl"])[:NAME_REPORT]),
                       internal_objects={"count": len(decoder.internal), "sample": sorted(decoder.internal)[:32]},
                       supervisor_reads=decoder.supervisor_reads,
                       reasons=reasons, reasons_truncated=decoder.reason_count > len(reasons),
                       reason_count=decoder.reason_count, trace_loss=decoder.trace_lost or decoder.loss["malformed"] > 0,
                       decoder_complete=not decoder.stopped and decoder.loss["malformed"] == 0,
                       complete=complete)
    # The tree union lives once, in the certificate; shards reference it (union_ref).
    certificate.update(reads=sorted(decoder.reads.values(), key=lambda r: (r["path"], r["kind"], r.get("errno", ""))),
                       directory_reads=[{"path": p, "members": m} for p, m in sorted(decoder.directories.items())],
                       external_inputs=[r for _, r in sorted(decoder.external.items())],
                       external_inputs_resolved=all(r["resolved"] for r in decoder.external.values()),
                       negative_lookups=sorted(decoder.negative, key=lambda r: (r["path_or_class"], r["sequence"])),
                       path_aliases=[r for _, r in sorted(decoder.aliases.items())])
    reference = "reports/process-tree-" + context["capture_group"] + ".json"
    shards = []
    for suite in context["suites"]:
        shard = {key: context[key] for key in BINDINGS}
        shard.update({key: suite[key] for key in ("suite_id", "attempt", "worker", "test_ids", "outcomes_ref")})
        prefix = suite["suite_id"] + "::"
        shard.update(schema="leaf.ci.readset.v1", nodeids=[t[len(prefix):] if t.startswith(prefix) else t for t in suite["test_ids"]],
                     trace_kind="linux-process-tree", python_only=False,
                     capture_epoch=context.get("capture_epoch"), capture_epoch_manifest=context.get("capture_epoch_manifest"),
                     process_tree_ref=reference, union_ref=reference,
                     process_tree_sha256=digest(certificate), attribution_scope="tree-union",
                     reads=[], directory_reads=[], external_inputs=[], negative_lookups=[], path_aliases=[],
                     generated_inputs=[], generated_inputs_resolved=True,
                     shared_state_dependencies=[], shared_state_closure_resolved=True,
                     external_dependency_classes=[],
                     external_inputs_resolved=certificate["external_inputs_resolved"], children_complete=children,
                     completion_marker=bool(decoder.root and decoder.root["record"]["exit"] and matched),
                     trace_loss=certificate["trace_loss"], decoder_complete=certificate["decoder_complete"],
                     capture_complete=complete, incomplete_reasons=reasons)
        shards.append(shard)
    paths = {} if decoder.stopped else decoder.external_paths
    for row in paths.values():
        row.update(size=decoder.observed_sizes.get(row["path"]), written=row["path"] in decoder.written_paths)
    return {"certificate": certificate, "shards": shards, "external_paths": paths,
            "decoder_peak_bytes": decoder.peak_bytes}


def bind_external_identities(result, table):
    """Bind supervisor evidence without serializing its private absolute paths.

    Rows live in the certificate's union; shard digests are recomputed after binding.
    """
    certificate = result["certificate"]
    for row in certificate["external_inputs"]:
        identity = table.get(row["path_token"], {})
        row.update({k: identity[k] for k in ("identity_sha256", "inventory_ref", "resolved", "reason", "size") if k in identity})
    certificate["external_inputs_resolved"] = all(row["resolved"] for row in certificate["external_inputs"])
    for shard in result["shards"]:
        shard.update(external_inputs_resolved=certificate["external_inputs_resolved"],
                     process_tree_sha256=digest(certificate))
    return result


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_outputs(result, out_dir):
    root = Path(out_dir)
    group = result["certificate"]["capture_group"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", group):
        raise ValueError("invalid_capture_group")
    _atomic(root / "reports" / ("process-tree-" + group + ".json"), result["certificate"])
    for shard in result["shards"]:
        suite = quote(str(shard["suite_id"]), safe="").replace(".", "%2E") or "%00"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", shard["worker"]) or type(shard["attempt"]) is not int or shard["attempt"] < 1:
            raise ValueError("invalid_output_identity")
        _atomic(root / "readsets" / suite / str(shard["attempt"]) / (shard["worker"] + "-process.json"), shard)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("decode")
    command.add_argument("--trace", required=True)
    command.add_argument("--context", required=True)
    command.add_argument("--context-key")
    command.add_argument("--out", required=True)
    command.add_argument("--limit", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        context = json.loads(Path(args.context).read_bytes().decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")))
        if args.context_key:
            context = context[args.context_key]
        limits = {}
        for limit in args.limit:
            name, value = limit.split("=", 1)
            limits[name] = int(value)
        result = decode(Path(args.trace).read_bytes(), context, limits)
        write_outputs(result, args.out)
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        # Do not echo exception messages: they can contain raw trace or paths.
        print("trace_process_tree: invalid input, context, limit or output", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
