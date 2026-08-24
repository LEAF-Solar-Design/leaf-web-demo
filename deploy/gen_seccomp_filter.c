/*
 * Emits the raw seccomp-BPF program `setpriv --seccomp-filter` expects
 * (a concatenation of `struct sock_filter` entries — exactly what
 * seccomp_export_bpf() writes) for the dwg2dxf cage.
 *
 * BUILD-TIME ONLY. Run once inside deploy/Dockerfile.app to produce
 * /usr/local/etc/leaf/seccomp-dwg2dxf.bpf; the compiler, libseccomp-dev and
 * this source are all purged in the same layer. The output file has no
 * runtime dependency on libseccomp — setpriv reads it as raw bytes and
 * calls prctl(PR_SET_SECCOMP, ...) itself.
 *
 * DENYLIST, not allowlist (see server/dwg_convert.py and
 * C:/tmp/r-0823/phase-0.5/RECEIPT-04-seccomp.md for the reasoning): a
 * 7-fixture strace sweep of the pinned dwg2dxf 0.14.8584 binary found 24
 * distinct syscalls, none of them below. Default ALLOW means a syscall this
 * sweep did not anticipate still runs — an allowlist would instead turn that
 * gap into a SIGSYS on a real customer drawing. What is denied is the set the
 * sweep proves the honest parser never touches: network I/O, process
 * creation, ptrace/process_vm_* introspection, namespace/mount escapes, and
 * the fileless-reexec pair (execveat + memfd_create).
 *
 * execve ITSELF is deliberately NOT denied. setpriv installs this filter on
 * its OWN process immediately before its OWN execve into dwg2dxf — that is
 * the one execve syscall every caged run makes, and seccomp cannot tell "the
 * sanctioned handoff" apart from "a later one" by syscall number alone, so
 * denying execve would break the cage's own exec chain, not just an
 * attacker's. This is not a hole: seccomp filters are exec-inherited and can
 * only ever gain restrictions, never lose them, so any process a corrupted
 * dwg2dxf did manage to execve into — a dropped shell, say — is STILL bound
 * by this exact same filter and still cannot open a socket, fork, or ptrace.
 * The outcomes execve would buy an attacker are already denied downstream.
 */
#include <errno.h>
#include <seccomp.h>
#include <stdio.h>

/* Denied with EPERM, not SCMP_ACT_KILL: the caller sees an ordinary "this
 * call failed" the way libc already handles a permission-denied syscall,
 * instead of the process dying by SIGSYS in a way indistinguishable in
 * server logs from an unrelated crash. Either way the attempt is refused;
 * EPERM keeps the failure legible. */
static const int DENY_SYSCALLS[] = {
    /* network: a converter has no legitimate reason to open a socket */
    SCMP_SYS(socket), SCMP_SYS(socketpair), SCMP_SYS(connect),
    SCMP_SYS(bind), SCMP_SYS(listen), SCMP_SYS(accept), SCMP_SYS(accept4),
    SCMP_SYS(sendto), SCMP_SYS(recvfrom), SCMP_SYS(sendmsg),
    SCMP_SYS(recvmsg), SCMP_SYS(sendmmsg), SCMP_SYS(recvmmsg),
    /* process creation: dwg2dxf is single-threaded and never forks */
    SCMP_SYS(clone), SCMP_SYS(clone3), SCMP_SYS(fork), SCMP_SYS(vfork),
    /* introspection / injection of siblings */
    SCMP_SYS(ptrace), SCMP_SYS(process_vm_readv), SCMP_SYS(process_vm_writev),
    /* fileless re-exec: write shellcode to an anonymous fd, exec it in
     * place. Both are unused by the sanctioned handoff (that uses plain
     * execve — see the file header) so denying them costs it nothing. */
    SCMP_SYS(execveat), SCMP_SYS(memfd_create),
    /* namespace / mount escape attempts: no legitimate use in a converter,
     * and --inh-caps=-all already denies the capability most of these need,
     * but a seccomp EPERM is a cheap, consistent second wall */
    SCMP_SYS(unshare), SCMP_SYS(setns), SCMP_SYS(mount), SCMP_SYS(umount2),
    SCMP_SYS(pivot_root), SCMP_SYS(chroot),
};

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <out.bpf>\n", argv[0]);
        return 2;
    }

    scmp_filter_ctx ctx = seccomp_init(SCMP_ACT_ALLOW);
    if (ctx == NULL) {
        fprintf(stderr, "seccomp_init failed\n");
        return 1;
    }

    size_t n = sizeof(DENY_SYSCALLS) / sizeof(DENY_SYSCALLS[0]);
    for (size_t i = 0; i < n; i++) {
        if (seccomp_rule_add(ctx, SCMP_ACT_ERRNO(EPERM),
                              DENY_SYSCALLS[i], 0) != 0) {
            fprintf(stderr, "seccomp_rule_add failed for entry %zu (syscall #%d)\n",
                    i, DENY_SYSCALLS[i]);
            seccomp_release(ctx);
            return 1;
        }
    }

    FILE *out = fopen(argv[1], "wb");
    if (out == NULL) {
        fprintf(stderr, "fopen(%s) failed\n", argv[1]);
        seccomp_release(ctx);
        return 1;
    }
    int rc = seccomp_export_bpf(ctx, fileno(out));
    fclose(out);
    seccomp_release(ctx);
    if (rc != 0) {
        fprintf(stderr, "seccomp_export_bpf failed: %d\n", rc);
        return 1;
    }
    return 0;
}
