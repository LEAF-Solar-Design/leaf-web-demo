# Warm executor runtime prototype

This directory implements a local Python 3.12 prototype of the instant
executor. `WarmExecutorSupervisor` starts its fixed pool before traffic. An
assignment validates the checked-in v1 schemas, verifies the immutable source
digest, and loads source once into an idle child. `POST /v1/invoke` only checks
the local binding, an Ed25519 compact JWS lease, and in-memory idempotency
before sending work to that already-running child.

Run the focused suite from the repository root:

```powershell
python -m unittest executor.runtime.tests.test_runtime
python -m unittest executor.runtime.tests.test_service
```

The HTTP interface is HTTP/1.1 and keeps connections alive. Its endpoints are
`POST /v1/control/assign`, `POST /v1/control/release`, `POST /v1/invoke`,
`GET /health`, and `GET /metrics`. Assignment input wraps the existing
`session-assignment`, `code-load`, and `catalog-entry` contract documents with
`source` field. The sanitized drawing context comes from the assignment schema.
Source bytes must hash to both
the code and artifact digests in all three documents.

The child strips its environment, has no parent credentials, denies filesystem
access, blocks network/subprocess/native-loading imports, allows only a small
standard-library import set, bounds output, and is replaced after a wall-time
timeout. Drawing context is loaded at assignment and passed as `intake` to
`run(intake, params)`. It is never fetched during invocation.

Lease verification uses the native `cryptography` Ed25519 provider so signature
checks do not consume the instant-call latency budget.

This is a trusted, read-only code profile. It is not hostile-code isolation and
does not claim live SLO evidence. The prototype does not use Redis, PostgreSQL,
broker, Docker, queues, object storage, or any credential-bearing client on the
direct invocation path.
