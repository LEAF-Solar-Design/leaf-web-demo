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
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
    -exec sed -i \
      -e 's|http://deb.debian.org|https://deb.debian.org|g' \
      -e 's|http://security.debian.org|https://security.debian.org|g' {} + \
 && apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

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
RUN chmod 0500 /app/server/start-app.sh

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
RUN ["/bin/sh", "-c", "test -f /app/scripts/reconcile_customization_authority.py && test -s /app/scripts/reconcile_customization_authority.py && test -f /app/scripts/reconcile_sessions_authority.py && test -s /app/scripts/reconcile_sessions_authority.py"]

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
    LEAF_AGENT_STORE=legacy \
    LEAF_INSTANT_EXECUTION_ENABLED=0 \
    LEAF_GUEST_CAP_STORE=memory \
    LEAF_AUTHOR_QUOTA_STORE=memory \
    LEAF_DRAWING_STORE=legacy \
    LEAF_UPLOAD_STORE=legacy \
    LEAF_SOURCE_SHA=${LEAF_SOURCE_SHA} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8130

# GET /api/health -> 200 (urlopen raises on non-2xx -> nonzero exit).
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8130/api/health', timeout=4)" || exit 1

# uvicorn binds 0.0.0.0 so the container is reachable on the compose network
# (python app.py already binds 0.0.0.0, but uvicorn is the documented run form).
CMD ["/app/server/start-app.sh"]
