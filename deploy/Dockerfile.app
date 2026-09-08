# syntax=docker/dockerfile:1
#
# Leaf platform — tenant-facing APP process (FastAPI async job spine + catalog +
# drawing write loop + NL router + usage/ops/tenant surfaces).
#
# Contract: RUN.md, server/README.md, server/CONTRACT-ADDENDUM.md §7-§16.
# Listens on :8130. NEVER holds the APS credential (that is the broker's job) —
# all APS-capable execution goes out over BROKER_URL. Author path goes to the
# harness over LEAF_AUTHOR_HARNESS_URL.
FROM python:3.12-slim AS app

WORKDIR /app

COPY deploy/gen_seccomp_filter.c /tmp/gen_seccomp_filter.c

# --- git: REQUIRED, not optional. ---------------------------------------------
# server/customization_service.py shells out to git against the tenant bare repo
# (`rev-parse --verify refs/heads/main`, `show`, and `worktree add` for
# effective_catalog_dir). python:3.12-slim ships no git, so without this every
# such call raises FileNotFoundError and the app answers a 503 for a repository
# the harness had already provisioned correctly.
# HTTPS mirrors, the harness Dockerfile's exact idiom: the self-hosted build
# runner's VPC has no port-80 egress, which only surfaces when a base-image
# bump busts the apt layer cache (two identical failures on 2026-08-04:
# "Unable to connect to deb.debian.org:http").
#
# --- dwg2dxf (GNU libredwg): the APS-free guest DWG read lane. ----------------
# server/dwg_convert.py runs it as a CAGED SUBPROCESS converting an
# uploaded .dwg to ASCII DXF for the dxf_intake parser (guest_uploads engine
# `local`; CONTRACT-ADDENDUM §19).
#   * Built from source because Debian trixie ships NO libredwg package
#     (verified against this exact base image 2026-08-10: `apt-cache policy/
#     search libredwg*` empty beside a known-positive control).
#   * Provenance, and why this is NOT the GNU FTP tarball any more. Upstream has
#     cut no stable release since 0.13.3; ftp.gnu.org/gnu/libredwg/ still ends
#     there (checked 2026-08-24). Everything after it ships as dated 0.14.NNNN
#     snapshots on the project's GitHub releases, which is the ONLY channel that
#     carries the post-0.13.3 memory-safety work. 0.13.3 is therefore not "the
#     stable choice", it is the last unmaintained point on a dead line.
#     The pinned sha256 below is the digest upstream publishes for this exact
#     tarball in the release's own `dist.sha256` asset, independently recomputed
#     from the download before pinning; `sha256sum -c` re-verifies it at build
#     time, so a mutated or substituted asset fails the BUILD.
#   * SECURITY (why the version moved): 0.13.3 carries CVE-2026-9605, a
#     heap-buffer-overflow whose fix (commit 8f03865f, "decode: fix
#     decompression overflow") lands in src/decode.c — the code path dwg2dxf
#     actually executes. 0.13.3 has only the partial `+32+info->size` bound
#     there; this pin has the full `+size > max_decomp_size` / `> dec.size`
#     guards. Keep this pin CURRENT: server/dwg_convert.py's cage bounds the
#     next parser bug, it does not excuse shipping a known one.
#     (CVE-2026-15182 is dwg_bmp/src/dwg.c — reached only by the `dwgbmp`
#     program, which this image does not install. Not reachable here.)
#   * LICENSE (GPL-3.0+): consumed strictly across a process boundary —
#     subprocess only, never linked or bound into the Python process (the
#     load-bearing note lives in server/dwg_convert.py). The binary ships only
#     inside this server image and is never distributed to end users.
#   * --disable-werror: GCC 14 (trixie) promotes -Walloc-size findings in
#     encode.c to errors; upstream ships this switch for exactly that. Still
#     required at 0.14.8584 (a build without it was not attempted; the flag is
#     carried forward, and the pinned build below is daemon-verified green with
#     it — `dwg2dxf 0.14.8584`, 2026-08-24).
#   * --disable-shared --disable-bindings: one static binary, no bindings.
#   * The toolchain is purged in the SAME layer; `dwg2dxf --version` at the
#     end is the build-time proof the binary landed and runs.
#   * make -j4, not -j"$(nproc)": the 2026-08-24 leaf-platform staging
#     outage traced to an unbounded -j compile starving a small runner's
#     CPU/RAM (MASTER-PLAN Phase 0.5). This build now only runs on the
#     leaf-gha-runner-web-demo CodeBuild project (BUILD_GENERAL1_LARGE,
#     8 vCPU / 15 GiB), but the cap is fixed rather than nproc-derived so
#     it stays safe on any runner class this image is ever built on.
#
# --- dwg2dxf syscall denylist (seccomp-bpf) -----------------------------------
# deploy/gen_seccomp_filter.c, compiled and RUN here against libseccomp-dev,
# emits the raw BPF program server/dwg_convert.py points setpriv's
# --seccomp-filter at. Build-time only: the compiler and libseccomp-dev are
# purged in this same layer, and the emitted .bpf file has no runtime
# dependency on libseccomp (setpriv reads it as raw bytes itself). See the
# generator's own header comment for what is denied and why execve is not.
#
# --- Debian security refresh (the Dockerfile.harness contract, on trixie). ----
# Apply Debian security updates at build time so the image never ships
# base-layer CVEs the distro has already fixed (the Trivy cve-harvest gate
# blocks HIGH/CRITICAL with a released fix; libexpat1 CVE-2026-56408 turned
# staging auto-deploys off for every merge until the harness picked up this
# exact contract, run 33465506870).
#
# The producer resolves both signed trixie channel documents and passes their
# exact SHA256 values as build arguments. They are cache-key inputs to the RUN
# below and members of the signed surface fingerprint, so a Debian channel
# update both invalidates the apt layer and refuses signed reuse of the
# pre-update image. The files apt downloaded must match before any package is
# upgraded; a missing, malformed, stale, or substituted value fails the build
# closed.
ARG TRIXIE_DEBIAN_SECURITY_INRELEASE_SHA256
ARG TRIXIE_DEBIAN_UPDATES_INRELEASE_SHA256

# NOTE: the `apt-get install ... git` prefix below is pinned verbatim by
# server/tests/test_postgres_container_wiring.py (_PINNED_GIT_INSTALL).
RUN printf '%s\n' "$TRIXIE_DEBIAN_SECURITY_INRELEASE_SHA256" \
      | grep -Eq '^[0-9a-f]{64}$' \
 && printf '%s\n' "$TRIXIE_DEBIAN_UPDATES_INRELEASE_SHA256" \
      | grep -Eq '^[0-9a-f]{64}$' \
 && find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
    -exec sed -i \
      -e 's|http://deb.debian.org|https://deb.debian.org|g' \
      -e 's|http://security.debian.org|https://security.debian.org|g' {} + \
 && apt-get update \
 && printf '%s  %s\n' "$TRIXIE_DEBIAN_SECURITY_INRELEASE_SHA256" \
      /var/lib/apt/lists/deb.debian.org_debian-security_dists_trixie-security_InRelease \
      | sha256sum -c - \
 && printf '%s  %s\n' "$TRIXIE_DEBIAN_UPDATES_INRELEASE_SHA256" \
      /var/lib/apt/lists/deb.debian.org_debian_dists_trixie-updates_InRelease \
      | sha256sum -c - \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
      xz-utils gcc make libc6-dev libseccomp-dev \
 && curl -fsSL https://github.com/LibreDWG/libredwg/releases/download/0.14.8584/libredwg-0.14.8584.tar.xz \
      -o /tmp/libredwg.tar.xz \
 && echo "23330d9f887ebb93ff9512751c0f77a905a16b11ba659787074469c8f1581402  /tmp/libredwg.tar.xz" \
      | sha256sum -c - \
 && tar -xJf /tmp/libredwg.tar.xz -C /tmp \
 && cd /tmp/libredwg-0.14.8584 \
 && ./configure --disable-shared --disable-bindings --disable-werror \
      > /tmp/libredwg-configure.log 2>&1 \
 && make -j4 > /tmp/libredwg-make.log 2>&1 \
 && install -s -m755 programs/dwg2dxf /usr/local/bin/dwg2dxf \
 && cd / \
 && rm -rf /tmp/libredwg-0.14.8584 /tmp/libredwg.tar.xz \
      /tmp/libredwg-configure.log /tmp/libredwg-make.log \
 && mkdir -p /usr/local/etc/leaf \
 && gcc -O2 -o /tmp/gen_seccomp_filter /tmp/gen_seccomp_filter.c -lseccomp \
 && /tmp/gen_seccomp_filter /usr/local/etc/leaf/seccomp-dwg2dxf.bpf \
 && test -s /usr/local/etc/leaf/seccomp-dwg2dxf.bpf \
 && rm -f /tmp/gen_seccomp_filter /tmp/gen_seccomp_filter.c \
 && apt-get purge -y curl xz-utils gcc make libc6-dev libseccomp-dev \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && dwg2dxf --version

# --- Python deps: server + platform + da (the three the app imports). ---------
# psycopg[binary] ships its own libpq wheel, so no apt libpq-dev is needed.
# PyJWT (server/requirements-auth.txt) is an OPERATOR OPT-IN for LEAF_AUTH_LIVE=1
# (see deploy/README.md) — the default demo never imports it.
COPY server/requirements.txt        /app/server/requirements.txt
COPY server/requirements-auth.txt   /app/server/requirements-auth.txt
COPY server/requirements-telemetry.txt /app/server/requirements-telemetry.txt
COPY platform/requirements.txt      /app/platform/requirements.txt
COPY da/requirements.txt            /app/da/requirements.txt
RUN pip install --no-cache-dir \
      -r /app/server/requirements.txt \
      -r /app/server/requirements-auth.txt \
      -r /app/server/requirements-telemetry.txt \
      -r /app/platform/requirements.txt \
      -r /app/da/requirements.txt

# --- Browser producer runtime: pinned Playwright + Chromium for the trusted-
# template JSON-to-CSV web tool (server/campaign_web_tool_producer.py). Root
# install only, run AFTER the distro security-pin apt gate above so that pin
# stays authoritative; the browser cache lands root-owned and world-readable+
# executable so the later `USER 10003:10003` drop can still launch Chromium,
# but never write into or replace it.
# Use an explicit mode for container hosts without nested browser sandboxing.
# It only permits the producer's exact server-owned template, never arbitrary HTML.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/leaf-browsers \
    LEAF_MANAGED_WEB_BROWSER_MODE=trusted-template-container
RUN pip install --no-cache-dir playwright==1.60.0 \
 && python -m playwright install --with-deps chromium \
 && chmod -R o+rX /opt/leaf-browsers

# --- App source. The app resolves siblings via PROJECT_ROOT = /app -----------
# (deps.py / broker.py compute SERVER_DIR.parent). Copy the same dirs the native
# flow sees so engine/registry.json, data/rooftop_demo.intake.json, da/store.py,
# and the platform package all resolve.
COPY server/    /app/server/
COPY da/        /app/da/
COPY engine/    /app/engine/
COPY platform/  /app/platform/
COPY contract/  /app/contract/
COPY data/      /app/data/
COPY scripts/reconcile_customization_authority.py /app/scripts/reconcile_customization_authority.py
COPY scripts/reconcile_sessions_authority.py /app/scripts/reconcile_sessions_authority.py
COPY server/start-app.sh /app/server/start-app.sh
# 0555 ROOT-OWNED, and the ownership is the point. This image now drops
# privilege (below), so the runtime uid must be able to EXECUTE its entrypoint —
# the previous 0500 root-owned mode would have made the container unable to
# start at all. The other way to fix that is `chown` to the runtime uid, and it
# is the WRONG fix: it would let the unprivileged process rewrite its own
# entrypoint. Root-owned + world-execute gives the process exactly the read and
# execute it needs and no write.
RUN chmod 0555 /app/server/start-app.sh

# --- Drop privilege. THE LAST ROOT INSTRUCTION IN THIS FILE. -------------------
# Deterministic scan finding D2: this image ran as root, alone among the four
# (broker 10001, harness 10002, canonical-worker 65532 all already drop). It is
# the compounding factor on F1: the DWG parser eats UNAUTHENTICATED bytes, so a
# memory-corruption exploit in it landed as ROOT-in-container. Dropping to an
# unprivileged uid turns that ceiling into a low-privilege account.
#
# /data MUST BE CREATED HERE, and this is not housekeeping. Every writable path
# the deployed task uses is under a TOP-LEVEL /data (LEAF_STORE_DIR
# /data/drawings, LEAF_UPLOADS_DIR /data/drawings/uploads, LEAF_TENANT_GIT_DIR
# /data/tenant-git, LEAF_CUSTOMIZATION_DB /data/state/customization.db, ...
# terraform leaf_platform.tf), no volume is mounted there, and the app mkdirs it
# at runtime. `/` is root-owned 0755, so uid 10003 CANNOT create a top-level
# directory: without this line the container starts and then fails every write.
# The app's own mkdir(parents=True) makes the subtree once /data exists.
#
# /app is deliberately left ROOT-OWNED and read-only to the runtime user. The
# app never writes there (PYTHONDONTWRITEBYTECODE=1 is set below, and the
# deployed task points every store at /data or postgres), so the process cannot
# modify its own code or its reconcilers.
RUN groupadd --system --gid 10003 leaf \
 && useradd --system --uid 10003 --gid 10003 --home-dir /nonexistent \
      --shell /usr/sbin/nologin leaf \
 && mkdir -p /data \
 && chown 10003:10003 /data \
 && chmod 0750 /data
USER 10003:10003

# The stdlib `platform` module is shadowed by /app/platform once /app is on
# sys.path; app.py loads the platform package under a `leaf_platform` alias and
# runs from the /app/server cwd exactly like the native flow, so `import platform`
# stays stdlib. Run from the server workdir.
WORKDIR /app/server

# --- Runtime proof that the reconcilers survived the build. -------------------
# The image is the only place the documented reconciliation commands ever run
# (platform/authority-inventory.json ships `python /app/scripts/reconcile_*.py`),
# so a script that failed to land fails in an operator cutover rather than here.
#
# This is the LAST instruction that can touch the filesystem, and it must stay
# that way: it proves the state of /app/scripts AFTER every RUN and COPY above
# it, so a later instruction that removed them breaks the BUILD. No static check
# can do this job -- a text scan of this file does not know the WORKDIR in effect
# at each line, so a relative `rm -rf scripts` under WORKDIR /app reads as
# harmless. That approach was tried and removed in dbd6e5d; see the module
# comment in server/tests/test_postgres_container_wiring.py.
#
# `-s` as well as `-f`, because `-f` alone accepts an EMPTY file: a truncation
# above this line (`: > .../reconcile_sessions_authority.py`) left both tests
# green while the operator's parity run became a program that exits 0 and emits
# no receipt. `-s` closes the zero-byte case and NOTHING WIDER. A one-byte
# overwrite (`RUN printf 'pass\n' > .../reconcile_sessions_authority.py`) is a
# valid Python program that exits 0, emits no receipt, and passes this guard.
# Nothing here decides file CONTENT, and no earlier check does either: the
# COPY-map guard refuses a RENAMING copy, which is not the same thing. Content
# is a review class, not a gate.
#
# ABOVE the per-commit ARG deliberately, and it belongs nowhere else. A RUN below
# that ARG re-executes on every merge, which
# scripts/test_build_platform_images_workflow.py forbids outright (it fails the
# build gate, not merely the cache). Three rules cover the positions between
# them: a destructive instruction above this guard is caught by the guard; a RUN
# below the ARG is caught by that invariant; and anything else below this guard
# is caught by the allowlist in test_the_image_asserts_its_own_reconcilers_at_build_time.
# EXEC FORM, and that is load-bearing rather than a style choice. Shell-form
# RUN executes through whatever SHELL is in effect, so `SHELL ["/bin/true"]`
# placed ABOVE this line ran the identical guard as `/bin/true -c "test -f ..."`
# -- exit 0, nothing tested, image shipped with both reconcilers deleted, and
# every static check still green. A review confirmed it by replaying the
# mutation. Exec form names the interpreter itself and ignores SHELL, so no
# instruction above the guard can change what the guard means.
RUN ["/bin/sh", "-c", "test -f /app/scripts/reconcile_customization_authority.py && test -s /app/scripts/reconcile_customization_authority.py && test -r /app/scripts/reconcile_customization_authority.py && test -f /app/scripts/reconcile_sessions_authority.py && test -s /app/scripts/reconcile_sessions_authority.py && test -r /app/scripts/reconcile_sessions_authority.py"]

# Declared below every non-consuming instruction, deliberately: this value is a
# new commit sha on every build, and a changed in-scope ARG is a buildx cache
# miss for everything after it — declared at the top it made the apt/pip layers
# uncacheable across commits (run 30983842725: predecessor cache imported,
# 0 layers CACHED).
ARG LEAF_SOURCE_SHA=unknown

ENV APS_LIVE=0 \
    APP_PORT=8130 \
    LEAF_JOBS_STORE=legacy \
    LEAF_SESSIONS_STORE=legacy \
    LEAF_SESSION_ANNEX_STORE=legacy \
    LEAF_AGENT_STORE=legacy \
    LEAF_INSTANT_EXECUTION_ENABLED=0 \
    LEAF_GUEST_CAP_STORE=memory \
    LEAF_AUTHOR_QUOTA_STORE=memory \
    LEAF_DRAWING_STORE=legacy \
    LEAF_UPLOAD_STORE=legacy \
    LEAF_SOURCE_SHA=${LEAF_SOURCE_SHA} \
    LEAF_DWG_CONVERT_REQUIRE_CAGE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8130

# GET /api/health -> 200 (urlopen raises on non-2xx -> nonzero exit).
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8130/api/health', timeout=4)" || exit 1

# uvicorn binds 0.0.0.0 so the container is reachable on the compose network
# (python app.py already binds 0.0.0.0, but uvicorn is the documented run form).
CMD ["/app/server/start-app.sh"]
