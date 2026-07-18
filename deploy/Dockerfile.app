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

# --- Python deps: server + platform + da (the three the app imports). ---------
# psycopg[binary] ships its own libpq wheel, so no apt libpq-dev is needed.
# PyJWT (server/requirements-auth.txt) is an OPERATOR OPT-IN for LEAF_AUTH_LIVE=1
# (see deploy/README.md) — the default demo never imports it.
COPY server/requirements.txt        /app/server/requirements.txt
COPY platform/requirements.txt      /app/platform/requirements.txt
COPY da/requirements.txt            /app/da/requirements.txt
RUN pip install --no-cache-dir \
      -r /app/server/requirements.txt \
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

# The stdlib `platform` module is shadowed by /app/platform once /app is on
# sys.path; app.py loads the platform package under a `leaf_platform` alias and
# runs from the /app/server cwd exactly like the native flow, so `import platform`
# stays stdlib. Run from the server workdir.
WORKDIR /app/server

ENV APS_LIVE=0 \
    APP_PORT=8130 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8130

# GET /api/health -> 200 (urlopen raises on non-2xx -> nonzero exit).
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8130/api/health', timeout=4)" || exit 1

# uvicorn binds 0.0.0.0 so the container is reachable on the compose network
# (python app.py already binds 0.0.0.0, but uvicorn is the documented run form).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8130"]
