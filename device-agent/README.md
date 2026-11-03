# Leaf device agent

Python 3.11+ outbound long-poll agent for
[`leaf.device-bridge.v1`](../contract/DEVICE-BRIDGE.md). macOS Apple Silicon is
the first installation target; Linux user services are supported. The Python
suite also runs on Windows without installing a service. Runtime dependencies
are stdlib and `cryptography`; its required `cffi` and `pycparser` dependencies
are included in the hash-pinned lock. Release wheel hashes come from
[PyPI](https://pypi.org/project/cryptography/50.0.0/#files); transitive hashes
are recorded with source URLs in `requirements.lock`.

## Install and operate

Obtain the checksum-bound `install.sh` from your trusted Studio release. Set
`LEAF_SERVER_ORIGIN` to its HTTPS origin and `LEAF_ENROLL_TOKEN` to the one-use
token using your shell's private input mechanism, then run `sh install.sh`.
Do not paste the token into a shell command that will enter history. The
installer prints the device UUID. It uses Python 3.13 from Homebrew when
available, otherwise an existing Python 3.11+, and installs a per-user venv
and launchd/systemd service. Download and pip TLS checks stay enabled. The
checksum binds the archive to the trusted installer; downloading both from
an untrusted source does not establish release trust.

The installer supports macOS ARM64 and Linux x86-64/ARM64 with wheels supplied
by the lock. It refuses source builds rather than fetching unpinned compilers
or build dependencies. A Linux user must have a working systemd user session;
enable lingering through the normal local administration process if the agent
must continue after logout. No root service or public listener is created.

Re-running the installer upgrades the venv and module in place, preserves the
existing identity and kill switch, refreshes the catalog from the trusted
release, and restarts the service. An already enrolled installation does not
consume another token. If registration reached Studio but its reply was lost,
obtain a new enrollment token; enrollment tokens cannot be reused.

Use the installed venv's Python for local commands:

```sh
python -m leaf_agent enroll --server https://studio.example.invalid
python -m leaf_agent run
python -m leaf_agent pause
python -m leaf_agent resume
python -m leaf_agent status
python -m leaf_agent install-service
python -m leaf_agent uninstall
```

`enroll` prefers `LEAF_ENROLL_TOKEN`; `--token` is available but exposes the
value in process arguments. `status` reports enrollment, device ID, and pause
state; it does not claim that the service is running. `uninstall` unloads the
user service and deletes the identity. Package data, logs and venv remain for
local inspection/removal. It does not revoke the Studio device record; use
Studio's revoke action for that. Revocation from any signed endpoint removes
the seed and config, clears pending results, and exits with code 3.

## Local files and execution

macOS home: `~/Library/Application Support/LeafAgent/`. Linux home:
`${XDG_DATA_HOME:-~/.local/share}/leaf-agent/`. `LEAF_AGENT_HOME` overrides both.

- `config.json`: server origin and device ID; no enrollment token.
- `device.key`: raw 32-byte Ed25519 seed, atomically created with private
  temporary-file permissions (0600 on POSIX; Windows uses owner-directory ACLs).
- `catalog.json`: release-pinned catalog copy; never populated by a command.
- `DISABLED`: every subsequent command is refused as paused; only local
  `resume` removes it. Polls and heartbeats continue.
- `results.sqlite3`: durable terminal-result deduplication and retry outbox.
- `agent.log`: rotating log, 1 MiB with five backups. Service stdout/stderr go
  to `stdout.log` and `stderr.log` in the same home.

The contract requires full heartbeats at least every **30 seconds**, which
takes precedence over the slice's suggested 60-second interval. Heartbeats
continue while an isolated operation is running. Network failures back off
from 1 to 60 seconds with deterministic device-derived jitter; each retry gets
a fresh nonce and signature. Studio origins require HTTPS and certificate
verification cannot be disabled. Fake-server tests inject a loopback transport
without changing the production origin or TLS rules.

Operations run sequentially in spawn-based child processes. `pkg.install`
has a 900-second deadline, all other operations 30 seconds. The worker is
terminated before another operation starts if it exceeds its deadline.
Completed results are committed before upload and replayed without repeating
the operation, including after a restart. A machine/process failure during an
operation before its terminal result is committed can still leave partial local
work; package handlers must make their own installations idempotent.

Proxy authorization repeats the catalog method/path check locally, rejects
encoded/noncanonical paths, encodes query values separately, never follows
redirects, and contacts only the catalog's `127.0.0.1` port. Bodies must be JSON
and fit within 1 MiB. Local auth comes exclusively from the handler. Upstream
JSON HTTP errors preserve their status; empty 204/304 results omit `body_json`.
Logs contain only fixed messages and HTTP status numbers, never request bodies,
tokens, signatures, keys, or upstream exception text.

## Package handler boundary

`packages.Registry` starts empty. This slice contains no production HA handler
and never enables the test-only `echo` package in a release. P5 can register a
trusted handler implementing `handle(op, args)`, `auth_headers()`, and `state()`.
HA operations implicitly select `homeassistant`. Registration must be explicit
in local agent startup code, never selected by remote import paths or plugins.

Handlers must be spawn-picklable, persist package state locally, and return
credential-free JSON. `state()` returns a contract `PackageState` or `None` for
an uninstalled package. It should be quick and must not expose credentials.
`auth_headers()` is used locally; its values and common credential fields are
also scrubbed from handler/proxy responses as defense in depth. Handlers own
any subprocess cleanup and must respect interruption; the core kills its op
worker, not arbitrary detached package processes. Unknown packages/versions and
operations fail closed; `agent.update` is reserved and refused.

## Build and verification

```sh
python device-agent/build_package.py --output /tmp/leaf-agent-release
python -P -m pytest device-agent/tests -q -p no:cacheprovider -o pythonpath=device-agent
```

The build emits `package.tar.gz`, prints its SHA-256, and renders `install.sh`
with that digest and agent version. Archive entries are sorted, have fixed
ownership/modes and zero timestamps, and exclude bytecode. The package installs
directly into the venv's purelib directory after hash-locked dependencies,
avoiding an unpinned build backend. Publish the archive at
`<origin>/api/devices/agent/package.tar.gz` alongside its matching trusted
installer.

The suite uses threaded fake Studio and local package HTTP servers, with an
independent signature verifier, the contract's worked signing vector, an
injected clock for retry budgets, real isolated workers for deadline behavior,
and repeated archive builds. Plist rendering and exact stubbed launchctl argv
are asserted on every platform; no actual launchctl/systemctl process is run
by the suite, so no platform skips are needed.
