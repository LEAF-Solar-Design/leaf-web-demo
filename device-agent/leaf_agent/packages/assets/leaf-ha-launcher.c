/* Main executable of "Leaf Home Assistant.app". macOS grants Local Network to
 * an app's own Mach-O, and a child inherits it; a launchd job or a script main
 * executable never gets it (finding F10). Reads Contents/Resources/run.args:
 * line 1 lan status path, line 2 log path, then argv, one per line.
 * Build: clang -O1 -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 -o leaf-ha-launcher leaf-ha-launcher.c
 * Keep the minimum: a binary built without it requires the build Mac's macOS, and
 * LaunchServices refuses it on older hubs with error -10825 (finding F20). */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <net/if.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_ARGS 32
extern char **environ;
static volatile sig_atomic_t stopping = 0;
static volatile pid_t child = 0;

/* A stop before the child exists sets the flag, so the launcher never spawns it. */
static void forward(int sig) {
    stopping = sig;
    if (child > 0) kill(child, sig);
}

/* One UDP datagram to the subnet broadcast triggers the Local Network prompt. */
static int lan_probe(void) {
    struct ifaddrs *list = NULL, *it;
    struct sockaddr_in to;
    int found = 0;
    if (getifaddrs(&list) != 0) return 0;
    for (it = list; it; it = it->ifa_next) {
        if (it->ifa_addr && it->ifa_addr->sa_family == AF_INET && (it->ifa_flags & IFF_BROADCAST)
            && (it->ifa_flags & IFF_UP) && !(it->ifa_flags & IFF_LOOPBACK) && it->ifa_broadaddr) {
            memcpy(&to, it->ifa_broadaddr, sizeof to);
            found = 1;
            break;
        }
    }
    freeifaddrs(list);
    if (!found) return 0;
    to.sin_port = htons(38899);
    int s = socket(AF_INET, SOCK_DGRAM, 0), on = 1, ok = 0;
    if (s < 0) return 0;
    setsockopt(s, SOL_SOCKET, SO_BROADCAST, &on, sizeof on);
    const char *m = "{\"method\":\"getPilot\",\"params\":{}}";
    for (int i = 0; i < 24 && !ok && !stopping; i++) { /* two minutes for the user to click Allow */
        ok = sendto(s, m, strlen(m), 0, (struct sockaddr *)&to, sizeof to) >= 0;
        if (!ok && !stopping) sleep(5); /* a signal ends the sleep early */
    }
    close(s);
    return ok;
}

int main(void) {
    char exe[PATH_MAX], path[PATH_MAX + 64], *lines[MAX_ARGS + 3], buf[16384];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0) return 78;
    char *slash = strrchr(exe, '/');
    if (!slash) return 78;
    *slash = 0; /* .../Contents/MacOS */
    snprintf(path, sizeof path, "%s/../Resources/run.args", exe);
    FILE *f = fopen(path, "r");
    if (!f) return 78;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    int count = 0;
    for (char *line = strtok(buf, "\n"); line && count < MAX_ARGS + 2; line = strtok(NULL, "\n")) lines[count++] = line;
    if (count < 3) return 78; /* fails closed on a malformed args file */
    lines[count] = NULL;

    /* Handlers first: a stop can arrive during the two minute probe. */
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    int ok = lan_probe();
    if (stopping) return 0;
    int st = open(lines[0], O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (st >= 0) { const char *v = ok ? "ok\n" : "denied\n"; write(st, v, strlen(v)); close(st); }

    int log = open(lines[1], O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (log >= 0) { dup2(log, 1); dup2(log, 2); close(log); }
    /* Block the signals across the spawn so one cannot land between spawn and
     * recording the pid; the child gets the default mask and handlers back. */
    sigset_t block, previous, empty;
    sigemptyset(&block);
    sigaddset(&block, SIGTERM);
    sigaddset(&block, SIGINT);
    sigemptyset(&empty);
    sigprocmask(SIG_BLOCK, &block, &previous);
    if (stopping) return 0;
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &empty);
    posix_spawnattr_setsigdefault(&attr, &block);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    pid_t pid;
    int spawned = posix_spawn(&pid, lines[2], NULL, &attr, &lines[2], environ);
    posix_spawnattr_destroy(&attr);
    if (spawned != 0) return 78;
    child = pid;
    sigprocmask(SIG_SETMASK, &previous, NULL);
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1; /* never spin on ECHILD */
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
