FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN userdel --remove user 2>/dev/null || true; \
    groupdel user 2>/dev/null || true; \
    groupadd --gid 1000 user; \
    useradd --create-home --uid 1000 --gid 1000 --shell /bin/sh user; \
    test "$(id -u user)" = "1000"

USER user
WORKDIR /home/user

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# This is a bare e2b sandbox template — no server runs by default, so the only
# honest liveness signal is that the interpreter itself still execs. Anything
# launched inside the sandbox owns its own health story.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import sys; sys.exit(0)" || exit 1
