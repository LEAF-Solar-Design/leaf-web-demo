"""Decode a sanitized, receipt-bound Linux process tree without filesystem inference.

Only local input/output filenames reach the host filesystem. Traced names are
POSIX strings interpreted against the launch inventory, never against this host.
Completeness is evidence in the documents, not the decode command's exit status.
Amendment 6: a positive POLICY-derived TRACED set is bound into syscall_policy_digest.
Amendment 6: successful path calls are dependencies; writable opens are also generated outputs.
Amendment 6: directory membership comes from the launch inventory, never d_name buffers.
Amendment 6: unknown descriptors are counters unless needed to resolve a path.
Amendment 6: interrupted calls touch no descriptors or dependencies and are not malformed.
Amendment 6: pairing uses (pid, name), one slot per pid; only path or creator gaps are reasons.
Amendment 6: live and total task caps are separate; reports retain at most 65,536 task records.
Amendment 6: PR_SET_VMA remains compatible and parser version s15a-6 changes the epoch.
Amendment 7: AT_EMPTY_PATH stats use descriptors and retain st_size or stx_size evidence.
Amendment 7: getdents proves directoryness; only consumed repo listings require EOF.
Amendment 7: indexed inventory directories and source-attributed bytecode are inputs.
Amendment 7: unlisted inputs are unresolved generated inputs, not capture loss.
Amendment 7: resumed endpoint outputs survive pairing; alias races require prior use.
Amendment 7: unknown fd samples are bounded; creation evidence (A7-10) is deferred.
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
PARSER_VERSION = "s15a-11"
# max_state_bytes: 2 GiB, compared against measured peak RSS (Amendment 5); the counter it
# replaced only ever added, so a 40 M-line stream tripped it on cumulative allocation.
LIMITS = dict(max_live_tasks=65536, max_total_tasks=4194304, max_fds_per_task=65536, max_record_bytes=1048576,
              max_dependencies=1000000, max_state_bytes=2 * 1024 ** 3, max_reasons=256)
# ru_maxrss is KiB on Linux, bytes on macOS; sampled once per RSS_INTERVAL lines.
RSS_SCALE, RSS_INTERVAL = (1 if sys.platform == "darwin" else 1024), 65536
# Calibration instrument bounds: distinct sanitized shapes kept, names counted, names reported.
SAMPLE_LIMIT, NAME_LIMIT, NAME_REPORT = 32, 4096, 32
# Raw bytes kept per sampled line; shaping collapses any quoted string the cut leaves open.
SAMPLE_PREFIX = 512
POLICY = {
    # Legacy decoder compatibility; the capture selects only the positive TRACED set below.
    "noise": ("brk madvise mprotect munmap arch_prctl set_tid_address set_robust_list get_robust_list rseq rt_sigaction "
              "rt_sigprocmask rt_sigreturn sigaltstack rt_sigsuspend rt_sigtimedwait rt_sigpending restart_syscall "
              "sched_yield nanosleep clock_nanosleep getpid getppid gettid getuid geteuid getgid getegid getgroups "
              "getrlimit prlimit64 getrusage times clock_gettime gettimeofday time getrandom sched_getaffinity "
              "sched_getparam sched_getscheduler futex").split(),
    "path": "open openat openat2 creat execve execveat readlink readlinkat access faccessat faccessat2 stat lstat newfstatat statx statfs getcwd chdir fchdir getxattr lgetxattr listxattr llistxattr truncate".split(),
    "descriptor": "read pread64 readv preadv preadv2 mmap mremap munmap mprotect lseek dup dup2 dup3 fcntl close pipe pipe2 socketpair write pwrite64 writev pwritev pwritev2 truncate ftruncate fsync fdatasync fadvise64 posix_fadvise readahead sync_file_range flock fgetxattr flistxattr sendfile copy_file_range".split(),
    "metadata_mutation": "chmod fchmod fchmodat chown fchown fchownat lchown utimensat futimesat".split(),
    "internal_object": "memfd_create timerfd_create signalfd signalfd4".split(),
    "fd_table": "dup dup2 dup3 fcntl close close_range pipe pipe2 socketpair socket accept accept4 memfd_create timerfd_create eventfd eventfd2 epoll_create epoll_create1 signalfd signalfd4 inotify_init inotify_init1 inotify_add_watch inotify_rm_watch pidfd_open pidfd_getfd".split(),
    "lifecycle": "fork vfork clone clone3 exit exit_group".split(),
    "legacy_stat": "fstat fstatfs".split(),
    "filesystem_events": "inotify_init inotify_init1 inotify_add_watch inotify_rm_watch".split(),
    "directory": "getdents getdents64 mkdir mkdirat rmdir unlink unlinkat rename renameat renameat2 link linkat symlink symlinkat".split(),
    "process": "fork vfork clone wait4 waitid exit exit_group setsid setpgid getpid getppid gettid".split(),
    "network": "socket connect bind listen accept accept4 sendto recvfrom sendmsg recvmsg sendmmsg recvmmsg getsockopt setsockopt getpeername getsockname shutdown".split(),
    "namespace": "unshare setns chroot pivot_root mount umount2".split(),
    "reject": "ptrace process_vm_readv process_vm_writev io_setup io_submit io_getevents io_cancel io_destroy io_uring_setup io_uring_enter io_uring_register open_by_handle_at name_to_handle_at splice tee vmsplice bpf userfaultfd shmget shmat shmdt shmctl semget semop semctl msgget msgsnd msgrcv msgctl".split(),
    # Modern calls decode normally; ENOSYS probes on older kernels are no-ops.
    "abi": "openat2 clone3 close_range pidfd_open pidfd_getfd faccessat2".split(),
    "runtime": "brk madvise arch_prctl set_tid_address set_robust_list get_robust_list futex rseq rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack rt_sigsuspend rt_sigtimedwait rt_sigpending restart_syscall kill tkill tgkill sched_yield nanosleep clock_nanosleep poll ppoll select pselect6 epoll_create epoll_create1 epoll_ctl epoll_wait epoll_pwait eventfd eventfd2 alarm setitimer getitimer getuid geteuid getgid getegid getgroups uname getrlimit prlimit64 getrusage times umask clock_gettime clock_getres gettimeofday time getrandom sched_getaffinity sched_getparam sched_getscheduler prctl ioctl sysinfo getpgrp getpgid setrlimit capget sched_setaffinity timer_create timer_settime timer_gettime timer_delete timerfd_settime timerfd_gettime mincore msync mlock munlock".split(),
    "ioctl": "TCGETS TCSETS TCSETSW TCSETSF TIOCGWINSZ TIOCGPGRP TIOCSPGRP FIONBIO FIOCLEX FIONCLEX FIONREAD".split(),
    "prctl": ("PR_SET_NAME PR_GET_NAME PR_SET_PDEATHSIG PR_GET_DUMPABLE PR_SET_DUMPABLE PR_CAPBSET_READ PR_SET_NO_NEW_PRIVS "
              "PR_GET_NO_NEW_PRIVS PR_SET_CHILD_SUBREAPER PR_GET_CHILD_SUBREAPER PR_SET_KEEPCAPS PR_GET_SECCOMP "
              "PR_SET_TIMERSLACK PR_GET_TIMERSLACK PR_SET_PTRACER PR_SET_VMA 0x53564d41").split(),
    "fcntl": ("F_DUPFD F_DUPFD_CLOEXEC F_SETFD F_GETFD F_GETFL F_SETFL F_GETLK F_SETLK F_SETLKW F_OFD_GETLK F_OFD_SETLK "
              "F_OFD_SETLKW F_GETPIPE_SZ F_SETPIPE_SZ F_GETOWN F_SETOWN F_GETSIG F_SETSIG F_ADD_SEALS F_GET_SEALS").split(),
    "open_flags": "O_RDONLY O_WRONLY O_RDWR O_APPEND O_ASYNC O_CLOEXEC O_CREAT O_DIRECT O_DIRECTORY O_DSYNC O_EXCL O_LARGEFILE O_NOATIME O_NOCTTY O_NOFOLLOW O_NONBLOCK O_NDELAY O_PATH O_SYNC O_TRUNC O_RSYNC O_TMPFILE".split(),
    "clone_flags": "CLONE_VM CLONE_FS CLONE_FILES CLONE_SIGHAND CLONE_PTRACE CLONE_VFORK CLONE_PARENT CLONE_THREAD CLONE_SYSVSEM CLONE_SETTLS CLONE_PARENT_SETTID CLONE_CHILD_CLEARTID CLONE_DETACHED CLONE_UNTRACED CLONE_CHILD_SETTID CLONE_IO SIGCHLD".split(),
    "nondeterminism_boundary": ["clock", "randomness", "scheduling"],
}
TRACED = list(dict.fromkeys(
    POLICY["path"] + POLICY["directory"] + POLICY["metadata_mutation"] + POLICY["lifecycle"]
    + POLICY["fd_table"] + [name for name in POLICY["network"] if name in {"connect", "bind"}]
    + POLICY["namespace"] + POLICY["reject"]))
# Both the policy and its ordered capture projection participate in the epoch digest.
POLICY["traced"] = TRACED
PATH_ARGS = {
    **{n: (0,) for n in "open creat execve readlink access stat lstat statfs chdir truncate mkdir rmdir unlink chroot getxattr lgetxattr listxattr llistxattr chmod chown lchown".split()},
    **{n: (1,) for n in "openat openat2 execveat readlinkat faccessat faccessat2 newfstatat statx mkdirat unlinkat fchmodat fchownat utimensat futimesat".split()},
    "rename": (0, 1), "link": (0, 1), "symlink": (0, 1),
    "renameat": (1, 3), "renameat2": (1, 3), "linkat": (1, 3), "symlinkat": (0, 2),
}
FD_ARGS = {
    **{n: (0,) for n in "read pread64 readv preadv preadv2 write pwrite64 writev pwritev pwritev2 fstat fstatfs fchdir lseek dup fcntl close getdents getdents64 ioctl ftruncate fsync fdatasync connect bind listen accept accept4 sendto recvfrom sendmsg recvmsg sendmmsg recvmmsg getsockopt setsockopt getpeername getsockname shutdown".split()},
    **{n: (0,) for n in "openat openat2 execveat readlinkat faccessat faccessat2 newfstatat statx mkdirat unlinkat pidfd_getfd".split()},
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
LOOKUPS = set("stat lstat newfstatat statx access faccessat faccessat2 readlink readlinkat statfs getxattr lgetxattr listxattr llistxattr".split())
MUTATIONS = set(POLICY["directory"]) - {"getdents", "getdents64"}
METADATA_MUTATIONS = set(POLICY["metadata_mutation"])
FD_LOOKUPS = {"fstat", "fstatfs", "fgetxattr", "flistxattr"}
KNOWN = frozenset().union(*(POLICY[k] for k in ("path", "descriptor", "directory", "process", "network", "namespace", "runtime",
                                                "metadata_mutation", "internal_object", "filesystem_events", "noise", "legacy_stat", "abi")))
FD_CREATORS = set(POLICY["fd_table"]) - {"close", "close_range", "inotify_add_watch", "inotify_rm_watch"}
PAIR_REQUIRED = set(PATH_ARGS) | set(POLICY["path"]) | {"connect", "bind", "fork", "vfork", "clone", "clone3"} | FD_CREATORS
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
    if name in POLICY["abi"] and event["errno"] == "ENOSYS":
        return
    if name in {"fstat", "newfstatat", "statx"}:
        size = re.search(r"\b(?:st_size|stx_size)=(\d+)(?:,|\})", raw)
        if size:
            event["observed_size"] = int(size[1])
    # A resumed tail carries no trustworthy argument indices. Entry arguments
    # are already sanitized in its unfinished partner.
    if resumed:
        # Evidence only: these tokens must not change the semantic flags below.
        event["witness_flags"] = [malformed_shape(token) for token in args
                                  if re.fullmatch(r"AT_[A-Z_]+(?:\|AT_[A-Z_]+)*", token)][:8]
        if name in {"pipe", "pipe2", "socketpair"}:
            for token in args:
                if re.fullmatch(r"\[\s*\d+(?:<[^>]*>)?\s*,\s*\d+(?:<[^>]*>)?\s*\]", token):
                    event["endpoints"] = [_fd(x) for x in _split(token[1:-1])]
                elif re.fullmatch(r"[A-Z][A-Z0-9_]*(?:\|[A-Z][A-Z0-9_]*)*", token):
                    event["flags"].extend(token.split("|"))
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
    if name in {"clone", "clone3", "openat2"}:
        token = args[0] if name == "clone3" else args[2] if name == "openat2" and len(args) > 2 else raw
        m = re.search(r"(?:^|[,{]\s*)flags=([^,}]+)(?:[,}]|$)", token)
        if not m:
            if name == "clone":
                raise ValueError("unsupported_kernel_abi")
            if event["errno"] is None:
                raise ValueError("malformed_line")
        else:
            value = m[1].strip()
            # Keep flag literals only, never arbitrary struct text or pointers.
            if not re.fullmatch(r"(?:[A-Z][A-Z0-9_]*|[0-9]+)(?:\|[A-Z][A-Z0-9_]*)*", value):
                value = "unknown"
            event["flags"] = [] if value == "0" else value.split("|")
            if name == "openat2":
                event["scalars"][2] = int(value) if value.isdecimal() else value
    if name == "close_range":
        for i, token in enumerate(args[:2]):
            if token == "~0U":
                event["scalars"][i] = 4294967295
            elif re.fullmatch(r"0x[0-9a-fA-F]+", token):
                event["scalars"][i] = int(token, 16)
    # Amendment 6: getdents buffers, including abbreviated and resumed ones, are not evidence.
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
    if body.startswith("????("):
        event.update(kind="interrupted", name="????")
        return event
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
    # Dying tasks can leave arbitrary argument fragments, including a resumed tail.
    if ret == "?":
        event["kind"] = "interrupted"
        return event
    _args(event, returned[1], resumed=bool(resumed))
    if returned[3] is not None:
        event["return_fd"] = _fd(ret + "<" + returned[3] + ">")
    return event


def _stream(data):
    # Iteration avoids a second transcript-sized splitlines allocation.
    start, sequence = 0, 0
    while start < len(data):
        end = data.find(b"\n", start)
        if end < 0:
            yield sequence, data[start:], False
            return
        yield sequence, data[start:end], True
        sequence += 1
        start = end + 1


def parse_stream(data):
    events, reasons, pending = [], [], {}
    for sequence, raw, terminated in _stream(data):
        if not terminated:
            reasons.append("trace_loss")
            continue
        try:
            event = _parse_line(raw.decode("ascii", "strict"), sequence)
            events.append(event)
            key = (event["pid"], event["name"])
            if event["kind"] == "unfinished":
                previous = pending.get(event["pid"])
                if previous is not None and previous[1] in PAIR_REQUIRED:
                    reasons.append("unpaired_syscall")
                pending[event["pid"]] = key
            elif event["kind"] == "resumed":
                previous = pending.pop(event["pid"], None)
                if previous != key and (event["name"] in PAIR_REQUIRED or previous and previous[1] in PAIR_REQUIRED):
                    reasons.append("unpaired_syscall")
            elif event["kind"] == "detached":
                reasons.append("trace_loss")
            elif event["kind"] in {"exit", "killed", "interrupted"}:
                # A task that exits mid-syscall never resumes it.
                pending.pop(event["pid"], None)
        except (ValueError, UnicodeError) as exc:
            reasons.append("malformed_string" if isinstance(exc, UnicodeError) else str(exc))
    if any(key[1] in PAIR_REQUIRED for key in pending.values()):
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
    followed_links: list = field(default_factory=list)
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
            result.followed_links.append(prefix)
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
        self.reason_samples, self.reason_sample_counts = [], {}
        self.reason_event, self.reason_fd, self.reason_task = {}, None, None
        self.stopped = False
        self.limit_trip, self.decoder_stop, self.witnesses = {}, None, {}
        self.stop_pids, self.stop_pending, self.stop_terminals = set(), set(), set()
        self.post_stop_resumes = 0
        self.active_at_stop = []
        self.pid_witnesses, self.birth_returns, self.interrupted_names = {}, [], []
        # Live-state estimate: rises on retain, falls on release; the cap reads measured RSS
        # where resource exists and this counter's peak only where it does not.
        self.state_bytes = self.state_peak = self.measured_peak = 0
        # Descriptor tables by identity: [table, live holders]; released when the last holder goes.
        self.tables = {}
        self.syscall_name, self.line = "other", (0, b"")
        self.trace_lost = False
        self.tasks, self.active, self.pending, self.unfinished = [], {}, {}, {}
        self.tasks_total = self.observed_exits = 0
        self.reads, self.external, self.negative, self.aliases = {}, {}, [], {}
        self.external_paths, self.written_paths, self.observed_sizes = {}, set(), {}
        self.directories, self.enumerations, self.created, self.internal = {}, [], set(), set()
        source = context["source_root"].rstrip("/")
        def relative(path):
            return path[len(source) + 1:] if path.startswith(source + "/") else path
        self.files = frozenset(relative(path) for path in context["inventory"]["files"])
        self.inventory_links = frozenset(relative(path) for path in context["inventory"]["symlinks"])
        self.dirs, self.children = {"."}, {}
        for path in self.files | self.inventory_links:
            parts = path.split("/")
            for i in range(len(parts)):
                parent = "/".join(parts[:i]) or "."
                self.dirs.add(parent)
                self.children.setdefault(parent, set()).add("/".join(parts[:i + 1]))
        self.children = {path: sorted(members) for path, members in self.children.items()}
        self.cache_dirs = frozenset((path.rsplit("/", 1)[0] + "/" if "/" in path else "") + "__pycache__"
                                    for path in self.files if path.endswith(".py"))
        self.generated_inputs, self.listings, self.consumed_directories = {}, {}, set()
        self.followed_links = {}
        self.links = {}
        self.supervisor_reads = 0
        self.unix_peers, self.unix_sockets = {}, []
        self.event_count = self.syscall_count = 0
        self.loss = dict(unpaired=0, malformed=0, interrupted=0, unknown_syscalls=0, oversize=0,
                         unknown_descriptors=0, failed_fd_ops=0)
        self.loss["unknown_fd_samples"] = []
        self.fd_sample = None
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

    def unpaired(self, name, raw, event=None):
        previous = self.reason_event, self.reason_fd, self.reason_task
        if event is not None:
            self.reason_context(event)
        self.count(self.unpaired_by_name, name)
        self.sample(self.unpaired_samples, raw)
        if name in POLICY["reject"]:
            self.reason("unsupported_syscall:" + name)
        if name in PAIR_REQUIRED:
            self.reason("unpaired_syscall")
        else:
            self.loss["unpaired"] += 1
        self.reason_event, self.reason_fd, self.reason_task = previous

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
        return not any(name in {"clone", "clone3", "fork", "vfork"} for _, name in self.unfinished)

    def name_root(self, pid):
        record = self.root["record"]
        record["pid"] = pid
        self.active[pid] = self.active.pop(None)
        for key in [key for key in self.unfinished if key[0] is None]:
            self.unfinished[(pid, key[1])] = self.unfinished.pop(key)

    def reason_context(self, event, task=None):
        self.reason_event = event
        self.reason_task = task or self.active.get(event.get("pid"))
        argument = next(iter(event.get("fds", {}).values()), None)
        self.reason_fd = argument["fd"] if argument else None

    def reason_line(self, sequence, raw):
        self.line = (sequence, raw)
        prefix = raw[:SAMPLE_PREFIX].decode("ascii", "replace")
        pid = re.match(r"(?:\[pid +(\d+)\] |(\d+) +)", prefix)
        self.reason_context({"sequence": sequence, "pid": int(pid[1] or pid[2]) if pid else None,
                             "name": line_kind(prefix)})

    def reason(self, value):
        if value == "unknown_path_base" and value not in self.witnesses:
            event, task, fd = self.reason_event, self.reason_task, self.reason_fd
            entry = task["fds"].get(fd) if task else None
            desc = entry["description"] if entry else {}
            paths = list(event.get("paths", {}).values())
            self.witnesses[value] = [{"resumed_shape": event.get("resumed_shape"),
                                     "resume_matched": event.get("resume_matched", False),
                                     "flags_token": event.get("witness_flags", event.get("flags", []))[:8],
                                     "descriptor_kind": desc.get("kind", "unknown"),
                                     "open_shape": desc.get("open_shape"),
                                     "pathname_emptiness": ("empty" if "" in paths else
                                                            "non-empty" if paths else "unknown")}]
        count = self.reason_sample_counts.get(value, 0)
        if count < 8 and (count or len(self.reason_sample_counts) < 32):
            event, fd, task = self.reason_event, self.reason_fd, self.reason_task
            sequence = event.get("sequence", self.line[0])
            entry = task["fds"].get(fd) if task is not None else None
            raw = self.line[1] if sequence == self.line[0] else b""
            shape = event.get("_reason_shape")
            if shape is None:
                shape = malformed_shape(raw[:SAMPLE_PREFIX].decode("ascii", "replace"))
            self.reason_samples.append({"reason": value, "sequence": sequence,
                                        "pid": event.get("pid"), "syscall": event.get("name"),
                                        "fd": fd, "fd_kind": (entry["description"]["kind"] if entry
                                                                 else "unknown" if fd is not None else None),
                                        "shape": shape})
            self.reason_sample_counts[value] = count + 1
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
            self.sample_fd()
        if value in self.reasons:
            return
        self.reason_count += 1
        if len(self.reasons) < self.limits["max_reasons"]:
            self.reasons.add(value)
        elif not self.stopped:
            self.reasons.remove(max(self.reasons))
            self.reasons.add("capture_limit_exceeded:max_reasons")
            self.record_stop("max_reasons")
            self.stopped = True
            self.reads.clear()
            self.external.clear()
            self.negative.clear()
            self.aliases.clear()
            self.directories.clear()

    def record_stop(self, name, accounting=None):
        event, task, fd = self.reason_event, self.reason_task, self.reason_fd
        entry = task["fds"].get(fd) if task else None
        desc = entry["description"] if entry else {}
        path = desc.get("resolution")
        directory = path.path if path is not None else None
        if name not in self.limit_trip:
            self.limit_trip[name] = {
                "limit": name, "threshold": self.limits[name], "accounting": accounting,
                "components": {"inventory_membership_entries": sum(map(len, self.directories.values())),
                               "reads": len(self.reads), "external_inputs": len(self.external),
                               "negative_lookups": len(self.negative), "directories": len(self.directories),
                               "generated_inputs": len(self.generated_inputs), "live_tasks": len(self.active),
                               "total_tasks": self.tasks_total, "fds_per_task": len(task["fds"]) if task else 0,
                               "record_bytes": getattr(self, "record_bytes", len(self.line[1])),
                               "state_bytes": self.peak_bytes, "reasons": self.reason_count},
                "syscall": event.get("name"), "pid": event.get("pid"), "descriptor": fd,
                "directory_shape": malformed_shape(json.dumps(directory)) if directory is not None else None,
                "directory_member_count": len(self.children.get(directory, [])),
                "entry_sequence": event.get("sequence", self.line[0]), "processing_sequence": self.line[0]}
        if self.decoder_stop is None:
            self.stop_pids = set(self.active)
            self.stop_pending = set(self.unfinished)
            self.decoder_stop = {"sequence": self.line[0], "reason": name,
                                 "pending_calls": [{"pid": pid, "syscall": call,
                                                    "entry_sequence": pending[0]["sequence"]}
                                                   for (pid, call), pending in list(self.unfinished.items())[:16]]}
            self.active_at_stop = [{"pid": pid, "birth_sequence": task["record"]["birth_sequence"],
                                    "parent_pid": task["record"]["parent"]["pid"] if task["record"]["parent"] else None}
                                   for pid, task in list(self.active.items())[:64]]

    def drain_diagnostic(self, raw):
        # Match only the bounded prefix. No dependency decoding or state mutation.
        text = raw[:SAMPLE_PREFIX].decode("ascii", "replace")
        match = re.match(r"(?:\[pid +(\d+)\] |(\d+) +)?(?:\d+\.\d+ )?(.*)", text)
        if not match:
            return
        pid = int(match[1] or match[2]) if match[1] or match[2] else (self.root["record"]["pid"] if self.root else None)
        if pid not in self.stop_pids:
            return
        tail = match[3]
        if re.match(r"(?:exit_group|exit)\(|\+\+\+ (?:exited|killed)", tail):
            self.stop_terminals.add(pid)
        resumed = re.match(r"<\.\.\. ([A-Za-z_]\w*) resumed>", tail)
        if resumed and (pid, resumed[1]) in self.stop_pending:
            self.stop_pending.remove((pid, resumed[1]))
            self.post_stop_resumes += 1

    def cap(self, name, accounting=None):
        self.record_stop(name, accounting)
        self.reason("capture_limit_exceeded:" + name)
        self.stopped = True
        # Discard the entire dependency payload, never a seemingly complete prefix.
        self.reads.clear()
        self.external.clear()
        self.negative.clear()
        self.aliases.clear()
        self.directories.clear()

    def alias_witness(self, resolution, event):
        if "unsupported_alias_race" in self.witnesses:
            return
        alias = resolution.absolute
        live = set()
        for owner in self.active.values():
            for fd, entry in owner["fds"].items():
                if alias in entry["description"].get("followed_links", ()):
                    live.add((id(owner["fds"]), fd))
        inflight = 0
        for pending, cwd, snapshot, owner, _, _ in self.unfinished.values():
            through = False
            for index, path in pending.get("paths", {}).items():
                argument = pending["fds"].get(index - 1, {})
                desc = snapshot.get(argument.get("fd")) or {}
                resolved = resolve_path(cwd, desc.get("path"), path, self.context, {"symlinks": self.links})
                through |= alias in resolved.followed_links
            inflight += through
        target = self.links.get(alias)
        if target is None:
            links = self.context["inventory"]["symlinks"]
            target = links.get(alias, links.get(resolution.path))
        self.witnesses["unsupported_alias_race"] = [{
            "alias_token": malformed_shape(json.dumps(alias)),
            "origin": "inventory" if resolution.path in self.inventory_links else "runtime",
            "target_token": malformed_shape(json.dumps(target)) if target is not None else None,
            "prior_follow_sequence": self.followed_links.get(alias),
            "mutation_entry_sequence": event["sequence"], "mutation_completion_sequence": self.line[0],
            "live_descriptors": len(live), "inflight_resolutions": inflight}]

    def unknown_pid_witness(self, pid, event):
        if pid in self.pid_witnesses:
            return
        if len(self.pid_witnesses) >= 3:
            # A stream of resolved early children must not hide the first orphan.
            replace = next((key for key, row in reversed(list(self.pid_witnesses.items()))
                            if row["queue_resolved"]), None)
            if replace is None:
                return
            self.witnesses["unexplained_pid"].remove(self.pid_witnesses.pop(replace))
        row = {"pid": pid, "first_sequence": event["sequence"],
               "pending_birth_calls": [{"pid": owner, "syscall": name, "entry_sequence": pending[0]["sequence"]}
                                       for (owner, name), pending in self.unfinished.items()
                                       if name in {"fork", "vfork", "clone", "clone3"}][:16],
               "interrupted_calls": list(self.interrupted_names), "birth_returns": list(self.birth_returns),
               "prior_generations": [task["record"]["birth_sequence"] for task in self.tasks
                                     if task["record"]["pid"] == pid][:16], "queue_resolved": False}
        self.pid_witnesses[pid] = row
        self.witnesses.setdefault("unexplained_pid", []).append(row)

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
        if len(self.active) >= self.limits["max_live_tasks"]:
            self.cap("max_live_tasks")
            return None
        if self.tasks_total >= self.limits["max_total_tasks"]:
            self.cap("max_total_tasks")
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
        task = dict(record=record, cwd=cwd, fds=fds, last=None, retained=self.tasks_total < 65536)
        self.tasks_total += 1
        if len(self.tasks) < 65536:
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
                self.reason_task, self.reason_fd = task, fd
                self.cap("max_fds_per_task")
                return
            self.retain(128)
        task["fds"][fd] = {"description": desc, "cloexec": cloexec}

    def closefd(self, task, fd):
        if task["fds"].pop(fd, None) is not None:
            self.release(128)

    def fd(self, task, argument, required=False):
        self.reason_task = task
        self.reason_fd = argument["fd"] if argument else None
        entry = task["fds"].get(argument["fd"]) if argument is not None else None
        desc = entry["description"] if entry else None
        if desc is None or desc["kind"] in {"deleted", "unknown"}:
            self.fd_sample = {"fd": argument["fd"] if argument else None, "syscall": self.syscall_name,
                              "root": task is self.root, "sequence": self.syscall_sequence}
            if required:
                self.reason("unresolved_file_descriptor")
            else:
                self.loss["unknown_descriptors"] += 1
                self.count(self.unknown_fd_by_name, self.syscall_name)
                self.sample_fd()
            self.fd_sample = None
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

    def sample_fd(self):
        if len(self.loss["unknown_fd_samples"]) < SAMPLE_LIMIT:
            self.loss["unknown_fd_samples"].append(self.fd_sample or {
                "fd": None, "syscall": self.syscall_name, "root": None, "sequence": self.line[0]})

    def resolve(self, task, event, index, nofollow=False):
        argument = event["fds"].get(index - 1)
        self.reason_fd = argument["fd"] if argument else None
        raw = event["paths"].get(index)
        if raw is None:
            self.reason("malformed_string")
            return Resolution()
        base = None
        if not raw.startswith("/") and index > 0 and index - 1 in event["fds"]:
            arg = event["fds"][index - 1]
            if arg["fd"] != "AT_FDCWD":
                desc = self.fd(task, arg, required=True)
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
        for link in resolution.followed_links:
            if _under(link, self.context["source_root"]):
                self.followed_links.setdefault(link, event["sequence"])
        return resolution

    def directory(self, path):
        if path not in self.directories:
            members = self.children.get(path, [])
            self.directories[path] = members
            self.retain(160 + sum(64 + len(member.encode("utf-8")) for member in members))

    def bytecode_source(self, path):
        match = re.fullmatch(r"(?:(.*)/)?__pycache__/([^/]+?)\.[^.\/]+(?:\.opt-\d+)?(?:-pytest-[\d.]+)?\.pyc", path)
        if match:
            source = (match[1] + "/" if match[1] else "") + match[2] + ".py"
            if source in self.files:
                return source
        return None

    def observed_size(self, resolution, event):
        if resolution.absolute and "observed_size" in event and resolution.absolute not in self.observed_sizes:
            self.observed_sizes[resolution.absolute] = event["observed_size"]
            self.retain(64)

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
        elif resolution.absolute in self.created and resolution.path is None:
            token = digest(resolution.absolute)[:16]
            added = token not in self.internal
            self.internal.add(token)
        elif resolution.path is not None:
            path = resolution.path
            source = self.bytecode_source(path)
            directory = path in self.dirs or path in self.cache_dirs or path in self.directories
            if directory:
                self.directory(path)
            elif resolution.absolute not in self.created and resolution.absolute not in self.written_paths:
                if not error:
                    parts = path.split("/")
                    for i in range(len(parts)):
                        parent = "/".join(parts[:i]) or "."
                        if parent in self.listings:
                            self.consumed_directories.add(parent)
                if source:
                    path, kind = source, "bytecode"
                elif path not in self.files and path not in self.inventory_links and not allow_missing:
                    if path not in self.generated_inputs:
                        self.generated_inputs[path] = {"path": path, "resolved": False}
                        self.retain(160)
                    if (len(self.generated_inputs) + len(self.reads) + len(self.external) + len(self.negative)
                            + len(self.directories) > self.limits["max_dependencies"]):
                        self.cap("max_dependencies", "directories+generated_inputs+reads+external_inputs+negative_lookups")
                    return
            row = {"path": path, "kind": kind}
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
        if len(self.reads) + len(self.external) + len(self.negative) + len(self.directories) + len(self.generated_inputs) > self.limits["max_dependencies"]:
            self.cap("max_dependencies", "directories+generated_inputs+reads+external_inputs+negative_lookups")
        if added:
            self.retain(160)

    def written(self, path):
        if path not in self.written_paths:
            self.written_paths.add(path)
            self.retain(64)

    def enumerate(self, desc, event):
        ret = event["return_value"]
        if ret is None or ret < 0:
            return
        if not desc or desc["kind"] != "file":
            return
        path = desc["resolution"].path
        if not desc.get("directory"):
            if desc.get("read_or_seek"):
                if path is not None:
                    self.directory(path)
                    self.listings.setdefault(path, False)
                return
            desc["directory"] = True
        if "enumeration" not in desc:
            desc["enumeration"] = dict(path=desc["path"], eof=False, started=False)
            self.enumerations.append(desc["enumeration"])
        enumeration = desc["enumeration"]
        enumeration["started"] = True
        enumeration["eof"] |= ret == 0
        if path is not None:
            self.directory(path)
            self.generated_inputs.pop(path, None)
            self.listings[path] = self.listings.get(path, False) or enumeration["eof"]
            if sum(len(m) for m in self.directories.values()) + len(self.reads) + len(self.external) + len(self.negative) > self.limits["max_dependencies"]:
                self.cap("max_dependencies", "inventory_membership_entries+reads+external_inputs+negative_lookups")
        else:
            self.edge(desc["resolution"], "stat", event)

    def policy(self, event, task):
        name, flags = event["name"], event["flags"]
        if name in POLICY["namespace"] or any(f.startswith("CLONE_NEW") for f in flags):
            self.reason("namespace_change")
        if name in POLICY["abi"] and event["errno"] == "ENOSYS":
            return False
        # syscall_0xNN / syscall_NNN (unnamed by strace) are never known.
        if name in POLICY["reject"] or name not in KNOWN:
            self.reason("unsupported_syscall:" + name)
            return False
        if name in {"ioctl", "fcntl", "prctl"}:
            subtype = event["scalars"].get(0 if name == "prctl" else 1, "unknown")
            if name == "prctl" and event.get("subtype_label") in {"PR_SET_VMA", "0x53564d41"}:
                subtype = event["subtype_label"]
            if subtype not in POLICY[name]:
                if name in self.subtypes:
                    # The reason keeps the bounded ':unknown' form; the counter keeps the name.
                    self.count(self.subtypes[name], event.get("subtype_label", "unknown"))
                self.reason("unsupported_syscall:" + name + ":" + str(subtype))
                return False
        if name in {"open", "openat", "openat2"}:
            index = 2 if name in {"openat", "openat2"} else 1
            value = event["scalars"].get(index)
            if isinstance(value, str):
                if any(flag not in POLICY["open_flags"] for flag in value.split("|")):
                    self.reason("unsupported_kernel_abi")
            elif value not in {0, 1, 2} and not (name == "openat2" and event["errno"]):
                self.reason("unsupported_kernel_abi")
        if name in {"clone", "clone3"} and any(flag not in POLICY["clone_flags"] and not flag.startswith("CLONE_NEW") for flag in flags):
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
            desc = self.fd(task, argument, required=name in {"fchmodat", "fchownat", "utimensat", "futimesat"})
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
        self.reason_context(event, task)
        name, ret, flags = event["name"], event["return_value"], event["flags"]
        success = ret is not None and ret >= 0
        self.syscall_name = name
        self.syscall_sequence = event["sequence"]
        if name in POLICY["abi"] and event["errno"] == "ENOSYS":
            return
        if event["errno"]:
            # A failed operation cannot prove that an absent descriptor was open.
            # dup2/dup3's destination is an output, not a required input.
            inputs = [arg["fd"] for i, arg in event["fds"].items()
                      if arg["fd"] != "AT_FDCWD" and not (name in {"dup2", "dup3"} and i == 1)
                      and not (name == "mmap" and "MAP_ANONYMOUS" in flags)]
            inputs.extend(event.get("poll_fds", []))
            if any(fd not in task["fds"] for fd in inputs):
                self.loss["failed_fd_ops"] += 1
                return
        if not self.policy(event, task):
            return
        if name in {"fork", "vfork", "clone", "clone3"} and success and ret > 0:
            birth_return = {"pid": event["pid"], "child_pid": ret, "syscall": name,
                            "entry_sequence": event["sequence"], "completion_sequence": self.line[0]}
            self.birth_returns.append(birth_return)
            del self.birth_returns[:-16]
            for witness in self.pid_witnesses.values():
                if not witness["queue_resolved"]:
                    witness["birth_returns"].append(birth_return)
                    del witness["birth_returns"][:-16]
            child = self.birth(ret, event["sequence"], task, flags)
            if child:
                if ret in self.pid_witnesses:
                    self.pid_witnesses[ret]["queue_resolved"] = True
                for queued, amount in self.pending.pop(ret, []):
                    self.release(amount)
                    self.process(queued)
            return
        if name in {"pidfd_open", "pidfd_getfd"}:
            if success:
                self.putfd(task, ret, {"kind": "internal"}, True)
            return
        if name == "close_range":
            if success:
                first, last = event["scalars"].get(0), event["scalars"].get(1)
                if not isinstance(first, int) or not isinstance(last, int):
                    self.reason("malformed_line")
                    return
                if "CLOSE_RANGE_UNSHARE" in flags:
                    table = {fd: dict(entry) for fd, entry in task["fds"].items()}
                    self.drop(task["fds"])
                    task["fds"] = table
                    self.hold(table)
                    self.retain(128 * len(table))
                for fd in list(task["fds"]):
                    if first <= fd <= last:
                        if "CLOSE_RANGE_CLOEXEC" in flags:
                            task["fds"][fd]["cloexec"] = True
                        else:
                            self.closefd(task, fd)
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
                           any(flag in flags for flag in ("MFD_CLOEXEC", "TFD_CLOEXEC", "SFD_CLOEXEC")))
            return
        if name in POLICY["filesystem_events"]:
            if name.startswith("inotify_init") and success:
                self.putfd(task, ret, {"kind": "endpoint"}, "IN_CLOEXEC" in flags)
            self.filesystem_events()
            return
        if name in METADATA_MUTATIONS:
            self.metadata_mutation(task, event)
            return
        if name in {"open", "openat", "openat2", "creat"}:
            if success and "O_TMPFILE" in flags:
                resolution = self.resolve(task, event, 1 if name in {"openat", "openat2"} else 0)
                if resolution.path is not None:
                    self.edge(resolution, "open", event, allow_missing=True)
                    self.written(resolution.absolute)
                token = digest([event["pid"], event["sequence"], ret])[:16]
                self.internal.add(token)
                self.putfd(task, ret, {"kind": "internal"}, "O_CLOEXEC" in flags)
                return
            index = 1 if name in {"openat", "openat2"} else 0
            resolution = self.resolve(task, event, index, nofollow="O_NOFOLLOW" in flags)
            writable = name == "creat" or bool(set(flags) & {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC"}) or event["scalars"].get(index + 1) in {1, 2}
            if success and writable:
                self.written(resolution.absolute)
            if "O_CREAT" in flags or name == "creat":
                if success:
                    if name == "creat" or "O_EXCL" in flags or "O_TRUNC" in flags or resolution.path not in self.files:
                        self.created.add(resolution.absolute)
            if not success:
                self.edge(resolution, "open", event)
            else:
                desc = dict(kind="file", path=resolution.absolute, resolution=resolution, flags=flags,
                            open_shape=event.get("_reason_shape", malformed_shape(self.raw_of(event).decode("ascii", "replace"))),
                            followed_links=tuple(resolution.followed_links),
                            deleted=False, written=False,
                            directory="O_DIRECTORY" in flags or resolution.path in self.dirs or resolution.path in self.cache_dirs)
                if desc["directory"] and resolution.path is not None:
                    self.directory(resolution.path)
                self.putfd(task, ret, desc, "O_CLOEXEC" in flags)
                if "return_fd" in event:
                    self.fd(task, event["return_fd"])
                self.edge(resolution, "open", event, allow_missing=desc["directory"] or resolution.absolute in self.created)
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
            index = 1 if name in {"newfstatat", "statx", "faccessat", "faccessat2", "readlinkat"} else 0
            if event["paths"].get(index) == "" and "AT_EMPTY_PATH" in flags:
                desc = self.fd(task, event["fds"].get(index - 1))
                if desc and desc["kind"] == "file":
                    self.edge(desc["resolution"], "stat", event)
                    if success:
                        self.observed_size(desc["resolution"], event)
                return
            nofollow = name in {"readlink", "readlinkat", "lstat", "lgetxattr", "llistxattr"} or "AT_SYMLINK_NOFOLLOW" in flags
            resolution = self.resolve(task, event, index, nofollow=nofollow)
            self.edge(resolution, "readlink" if name.startswith("readlink") else "stat", event)
            if success:
                self.observed_size(resolution, event)
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
            indices = PATH_ARGS.get(name, ())
            if name.startswith("symlink"):
                indices = indices[-1:]
            resolutions = [self.resolve(task, event, i, nofollow=True) for i in indices]
            for resolution in resolutions:
                if success and (not name.startswith(("link", "symlink")) or resolution is resolutions[-1]):
                    self.written(resolution.absolute)
                if success and (resolution.path in self.inventory_links or
                                resolution.path is not None and resolution.absolute in self.followed_links):
                    self.alias_witness(resolution, event)
                    self.reason("unsupported_alias_race")
            if success and resolutions:
                if name.startswith(("mkdir", "rename", "link", "symlink")):
                    target = resolutions[-1]
                    self.created.add(target.absolute)
                    if name.startswith("symlink"):
                        self.links[target.absolute] = event["paths"][0]
                    if name.startswith("link"):
                        origin = resolutions[0]
                        if origin.absolute in self.links:
                            self.links[target.absolute] = self.links[origin.absolute]
                        elif origin.absolute in self.created:
                            pass
                        elif origin.path in self.files:
                            # The new name carries the inventory file's bytes: the origin is read.
                            self.edge(origin, "open", event)
                        else:
                            self.reason("unsupported_syscall:" + name + ":unwitnessed_origin")
                    if name.startswith("rename"):
                        origin = resolutions[0]
                        link = self.links.pop(origin.absolute, None)
                        self.links.pop(target.absolute, None)
                        if link is not None:
                            self.links[target.absolute] = link
                if name.startswith("unlink"):
                    self.links.pop(resolutions[0].absolute, None)
                if name.startswith(("unlink", "rename")):
                    for owner in self.active.values():
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
        desc = self.fd(task, argument, required=name == "fchdir")
        if desc is None:
            if success and (name in {"dup", "dup2", "dup3"} or name == "fcntl" and event["scalars"].get(1) in {"F_DUPFD", "F_DUPFD_CLOEXEC"}):
                self.closefd(task, ret)
            return
        fd = argument["fd"]
        if name in READS | FD_LOOKUPS | {"mmap"} and success:
            if desc["kind"] == "file":
                if name in READS:
                    desc["read_or_seek"] = True
                self.observed_size(desc["resolution"], event)
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
        elif name == "lseek" and success:
            desc["read_or_seek"] = True
        elif name == "close" and success:
            self.closefd(task, fd)

    def process(self, event):
        if self.stopped:
            return
        self.reason_context(event)
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
        self.reason_task = task
        if task is None or task["record"]["exit"] is not None:
            self.unknown_pid_witness(pid, event)
            event["_reason_shape"] = malformed_shape(self.raw_of(event).decode("ascii", "replace"))
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
            self.drop(task["fds"])
            task["fds"] = {}
            self.active.pop(pid)
            self.observed_exits += 1
            if not task["retained"]:
                self.release(256)
            return
        if kind == "signal":
            return
        key = (pid, event["name"])
        if kind == "interrupted":
            self.interrupted_names.append(event["name"])
            del self.interrupted_names[:-16]
            for witness in self.pid_witnesses.values():
                if not witness["queue_resolved"] and len(witness["interrupted_calls"]) < 16:
                    witness["interrupted_calls"].append(event["name"])
            self.loss["interrupted"] += 1
            for previous in [key for key in self.unfinished if key[0] == pid]:
                self.release(self.unfinished.pop(previous)[5])
            return
        if kind == "unfinished":
            for previous in [key for key in self.unfinished if key[0] == pid]:
                old = self.unfinished.pop(previous)
                self.release(old[5])
                self.unpaired(previous[1], old[4], old[0])
            # Capture the entry cwd and the descriptions of the descriptors this call uses only.
            snapshot = {fd: (task["fds"].get(fd) or {}).get("description") for fd in self.used_fds(event)}
            event.setdefault("_reason_shape", malformed_shape(raw.decode("ascii", "replace")))
            amount = 256 + len(raw) + 64 * len(snapshot)
            self.unfinished[key] = (event, task["cwd"]["path"], snapshot, task, raw, amount)
            self.retain(amount)
            return
        if kind == "resumed":
            pending = self.unfinished.pop(key, None)
            if pending is None:
                for previous in [key for key in self.unfinished if key[0] == pid]:
                    old = self.unfinished.pop(previous)
                    self.release(old[5])
                    self.unpaired(previous[1], old[4], old[0])
                self.unpaired(event["name"], raw)
                return
            original, cwd, snapshot, owner, _, amount = pending
            self.release(amount)
            joined = dict(original)
            joined["resume_matched"] = True
            joined["resumed_shape"] = malformed_shape(raw.decode("ascii", "replace"))
            joined["witness_flags"] = list(dict.fromkeys(original.get("flags", []) + event.get("witness_flags", [])))[:8]
            for field_name in ("return_value", "return_fd", "errno", "duration", "observed_size"):
                if field_name in event:
                    joined[field_name] = event[field_name]
            if event["endpoints"]:
                joined["endpoints"] = event["endpoints"]
            joined["flags"] = list(dict.fromkeys(original["flags"] + event["flags"]))
            joined["kind"] = "syscall"
            joined["entry_cwd"] = cwd
            self.reason_context(joined, owner)
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
                del partial[SAMPLE_PREFIX:]
                partial.extend(chunk[start:stop][:max(0, SAMPLE_PREFIX - len(partial))])
            if end < 0:
                break
            if length > decoder.limits["max_record_bytes"]:
                decoder.reason_line(sequence, bytes(partial))
                decoder.record_bytes = length
                decoder.loss["oversize"] += 1
                decoder.cap("max_record_bytes")
                partial.clear()
            yield sequence, bytes(partial), True
            sequence += 1
            partial.clear()
            length = 0
            start = end + 1
    if length:
        if length > decoder.limits["max_record_bytes"]:
            decoder.reason_line(sequence, bytes(partial))
            decoder.record_bytes = length
            decoder.loss["oversize"] += 1
            decoder.cap("max_record_bytes")
            partial.clear()
        yield sequence, bytes(partial), False


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
        decoder.reason_line(sequence, raw)
        decoder.event_count = sequence + 1
        if sequence % RSS_INTERVAL == 0:
            decoder.measure()
        if not terminated:
            decoder.reason("trace_loss")
            continue
        if len(raw) > caps["max_record_bytes"]:
            decoder.loss["oversize"] += 1
            decoder.cap("max_record_bytes")
        if decoder.stopped:
            decoder.drain_diagnostic(raw)
            continue
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
        decoder.unpaired(name, pending[4], pending[0])
    if decoder.pending:
        decoder.reason_context(next(iter(decoder.pending.values()))[0][0])
        decoder.reason("unexplained_pid")
    if decoder.root is None and not decoder.stopped:
        decoder.reason("missing_terminal")
    missing_exits = []
    for task in decoder.active.values():
        if task["record"]["exit"] is None:
            last = task["last"]
            decoder.reason_context({"pid": task["record"]["pid"],
                                    "sequence": last[0] if last else task["record"]["birth_sequence"],
                                    "name": line_kind(last[1].decode("ascii", "replace")) if last else None,
                                    "_reason_shape": malformed_shape(last[1].decode("ascii", "replace")) if last else ""}, task)
            decoder.reason("missing_terminal" if task is decoder.root else "descendant_outlived_tree")
            if len(missing_exits) < SAMPLE_LIMIT:
                record, last = task["record"], task["last"]
                missing_exits.append({"pid": record["pid"], "birth_sequence": record["birth_sequence"],
                                      "parent_pid": record["parent"]["pid"] if record["parent"] else None,
                                      "last_sequence": last[0] if last else None,
                                      "last_shape": malformed_shape(last[1].decode("ascii", "replace")) if last else None})
    if not decoder.stopped and any(not desc["verified_peer"] for desc in decoder.unix_sockets):
        decoder.network_input()
    if not decoder.stopped and any(not decoder.listings[path] for path in decoder.consumed_directories):
        decoder.reason("directory_enumeration_incomplete")
    sha = counters["hash"].hexdigest()
    receipt = context.get("terminal_receipt", {})
    matched = isinstance(receipt, dict) and receipt.get("byte_count") == counters["byte_count"] and receipt.get("sha256") == sha
    if not matched or not counters["terminated"]:
        decoder.reason_line(*decoder.line)
        decoder.reason("trace_loss")
    if decoder.stopped:
        decoder.reads.clear()
        decoder.external.clear()
        decoder.negative.clear()
        decoder.aliases.clear()
        decoder.directories.clear()
    reasons = sorted(decoder.reasons)
    records = [task["record"] for task in decoder.tasks]
    exits = decoder.observed_exits
    children = not any(task is not decoder.root for task in decoder.active.values()) and not decoder.pending and not decoder.stopped
    complete = decoder.reason_count == 0 and matched and exits == decoder.tasks_total and decoder.root is not None
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
                       seed_fds=seeds, tasks=records, tasks_total=decoder.tasks_total,
                       tasks_truncated=decoder.tasks_total > len(records), expected_exits=decoder.tasks_total, observed_exits=exits,
                       event_count=decoder.event_count, syscall_count=decoder.syscall_count,
                       transcript_bytes=counters["byte_count"], transcript_sha256=sha, receipt_matched=matched,
                       capture_epoch=context.get("capture_epoch"), capture_epoch_manifest=context.get("capture_epoch_manifest"),
                       syscall_policy_digest=digest(POLICY), loss_counters=decoder.loss,
                       reason_samples=decoder.reason_samples,
                       limit_trip=decoder.limit_trip, decoder_stop=decoder.decoder_stop,
                       post_stop={"terminals_seen": len(decoder.stop_terminals),
                                  "terminals_missing": len(decoder.stop_pids - decoder.stop_terminals),
                                  "resumes_matched": decoder.post_stop_resumes,
                                  "pids_active_at_stop": len(decoder.stop_pids)},
                       active_at_stop=decoder.active_at_stop, witnesses=decoder.witnesses,
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
    generated = []
    generated_inputs = []
    if not decoder.stopped:
        for path in sorted(p for p in decoder.written_paths if p is not None):
            resolution = resolve_path(None, None, path, context, {})
            if resolution.path is not None:
                generated.append({"path": resolution.path})
        generated_inputs = [row for path, row in sorted(decoder.generated_inputs.items())
                            if context["source_root"].rstrip("/") + "/" + path not in decoder.written_paths]
    certificate.update(generated_outputs=generated,
                       generated_inputs=generated_inputs, generated_inputs_resolved=not generated_inputs,
                       reads=sorted(decoder.reads.values(), key=lambda r: (r["path"], r["kind"], r.get("errno", ""))),
                       directory_reads=[{"path": p, "members": m, "complete": decoder.listings.get(p, True)}
                                        for p, m in sorted(decoder.directories.items())],
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
                     generated_inputs=generated_inputs, generated_inputs_resolved=not generated_inputs,
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
