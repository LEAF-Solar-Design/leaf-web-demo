# Device bridge wire contract

Contract: `leaf.device-bridge.v1`. The JSON Schema in
`device-bridge.v1.schema.json` defines message shapes. This document defines
authorization, timing, execution, and transport rules that a shape cannot check.

## Overview and threat model

The owner uses a browser to manage a project in Leaf Automation Studio. The
Studio FastAPI server authorizes that owner and holds a Postgres command queue.
A device agent runs on the owner's machine. The first target is a Mac mini
running Home Assistant (HA). An ops-dashboard connector executor uses a relay
token to call a small set of HA APIs through Studio.

The agent connects **outbound only**, over HTTPS on port 443. It does not expose
a public listener. Transport is HTTPS long-poll, not WebSocket. Studio runs
several ECS tasks behind an ALB. Any task can enqueue or lease a command from
Postgres; commands, results, revocation, and nonce checks must not depend on
task-local memory. A poll holds for at most 25 seconds, below the ALB's
60-second idle timeout. Each device has at most one outstanding poll.

Trust boundaries separate the browser and Studio, Studio and the agent, the
agent and localhost packages, and the connector executor and the relay. TLS
authenticates Studio to the agent. Ed25519 signatures authenticate the device
to Studio. The private device key stays on the device. HA credentials stay
there too; the agent supplies them locally and never returns them to Studio.

A compromised Studio server can request only the operations listed below, on
packages and versions in the agent's pinned catalog. This includes installing,
upgrading, removing, or controlling those packages, and pausing the agent.
It cannot request shell execution, an executable URL, an arbitrary file path,
proxy port/host, or a new catalog entry. Catalog updates require a trusted
agent release;
a command or heartbeat cannot replace the pinned copy. Reserved agent updates
must also pass the agent's trusted release verification; a supplied checksum
alone does not authorize executable code. An agent may refuse them entirely.
This boundary does not protect against vulnerabilities in an allowed package.

A stolen relay token grants only the declared method/path allowlist for one
package on one device. It can read HA state and control the allowed light,
scene, and homeassistant services. It grants no enrollment, package management,
HA configuration-flow, audit, or cross-device access. Revoke it to end access.
The connector executor is a bearer-token client, not a trusted device agent.

## Shared encoding and authorization rules

JSON uses UTF-8. All protocol objects reject unknown fields. Opaque JSON data
(`JsonValue`, used for HA data and proxy bodies) may contain arbitrary JSON
keys; those keys do not extend the wire envelopes. Times ending in `_at` are
RFC 3339 UTC strings, for example `2026-11-02T10:00:00Z`. Identifiers for
devices, enrollments, commands, and relay tokens are UUID strings. Base64url
values use the URL-safe alphabet and omit padding. SHA-256 strings are
64 lowercase hexadecimal characters. Versions are nonempty opaque release
identifiers, never download locations. Package names match
`^[a-z][a-z0-9-]{0,63}$`.

Every user endpoint uses the existing Auth0 tenant bearer and `require_tenant`.
Every project, device, command, enrollment, audit event, and relay token lookup
is tenant- and project-scoped. Cross-tenant and inaccessible identifiers return
404 `not_found`, without revealing existence. Listing and audit access require
project read access. Enrollment and command creation also require the project
owner and the existing `link_service` capability. Revoke and relay-token
creation/deletion require that same owner and capability gate. Command lookup
requires project read access. No device or relay credential grants user access.

Errors use `{"error":{"code":"<code>","message":"<safe explanation>"}}`.
Common endpoint errors are 400 `invalid_request` for malformed syntax/query,
401 `unauthorized` for a missing/invalid bearer, 403 `forbidden` for a failed
owner/capability check, 404 `not_found`, 422 `invalid_payload` for a schema
failure, 429 `rate_limited`, and 503 `service_unavailable` for unavailable
queue/storage. Error messages, access logs, and traces must redact credentials
and request bodies. `Cache-Control: no-store` applies to credential responses.
The tables give successful statuses and endpoint-specific failures.

## Endpoints

`User` below means the tenant-scoped rules above. `Device` means request signing
as specified in the next section. Routes accept no undocumented body fields.

| Method and path | Auth | Request | Success | Specific errors |
| --- | --- | --- | --- | --- |
| `POST /api/devices/enrollments` | User, owner + `link_service` | `{project_id, label}`; both nonempty strings, at most 128 characters | 201 `EnrollmentResponse` = `{enrollment_id, token, expires_at, install_command}` | Common errors |
| `POST /api/devices/agent/register` | Enrollment token only; no Auth0 or device signature | `RegisterRequest` = `{token, public_key, agent_version, platform:{os, os_version, arch}, hostname}` | 201 `{device_id, server_time}` | 401 `enrollment_invalid` for unknown, reused, or expired token; 409 `device_key_in_use` for an already enrolled public key |
| `GET /api/devices/agent/poll?wait=25` | Device | No body; integer `wait` in 0..25, default 25 | 200 `PollResponse` = `{commands:[Command]}` with 0..8 commands; 204 with no body when the hold expires | Signing errors; 401 `device_revoked`; 409 `poll_in_progress` |
| `POST /api/devices/agent/results` | Device | `ResultsRequest` = `{results:[Result]}`, 1..8 | 200 `{accepted:n}` | Signing errors; 401 `device_revoked`; 404 `not_found` for a command not assigned to this device; 409 `result_conflict` |
| `POST /api/devices/agent/heartbeat` | Device | `HeartbeatRequest` = `{agent_version, packages:[PackageState], free_mem_mb, uptime_s}` | 200 `{server_time}` | Signing errors; 401 `device_revoked` |
| `GET /api/devices?project_id=<project_id>` | User | No body; required project ID | 200 `{devices:[DeviceView]}` | Common errors |
| `GET /api/devices/{device_id}/audit?limit=<limit>` | User | No body; integer limit 1..200, default 50 | 200 `{events:[AuditEvent]}`, newest first | Common errors |
| `POST /api/devices/{device_id}/commands` | User, owner + `link_service` | `{op, args}`, exactly as in `Command` | 202 `{command_id}` | 409 `device_revoked`; 422 `unsupported_op`, `package_unknown`, `version_unknown`, or `path_denied` |
| `GET /api/devices/{device_id}/commands/{command_id}` | User | No body | 200 `{command:Command, result:Result|null}`; null until terminal | Common errors |
| `POST /api/devices/{device_id}/revoke` | User, owner + `link_service` | No body | 200 `{revoked_at}`; repeated requests return the original time | Common errors |
| `POST /api/devices/{device_id}/relay-tokens` | User, owner + `link_service` | `{package, label}`; package must have a relay allowlist, label is 1..128 characters | 201 `{relay_token, relay_origin, path_prefix}` | 409 `device_revoked`; 422 `package_unknown` |
| `DELETE /api/devices/{device_id}/relay-tokens/{token_id}` | User, owner + `link_service` | No body | 204, no body; idempotent for a known token | Common errors |
| `GET /api/devices/relay-openapi/{package}.json` | Public | No body | 200 deterministic OpenAPI JSON bytes; `X-Leaf-Document-Sha256` header | 404 `package_unknown` |
| `ANY /api/device-relay/{package}/{path...}` | Relay bearer only | Optional JSON body and query; accepts and drops `Idempotency-Key`; rules below | Upstream status, `content-type`, JSON body | 401 `relay_token_invalid`; 403 `relay_token_mismatch` or `relay_path_denied`; 413 `payload_too_large`; 415 `json_required`; 502 `upstream_error`; 503 `device_offline`; 504 `device_timeout` |
| `GET /api/devices/packages` | User, any tenant member | No body | 200 `{packages:[PackageCatalogEntry]}`; server catalog with the pinned install version | Common errors |
| `GET /api/devices/agent/package.tar.gz` | Public | No body | 200 deterministic agent archive; immutable cache-control and SHA-256 ETag | Common errors |
| `GET /api/devices/agent/install.sh` | Public | No body; allowed public origin from request host/forwarded host | 200 POSIX installer with public origin and archive SHA-256 | 400 `invalid_request` for an unapproved origin |

### P2 route behavior amendment

Concurrent polls from the same device are permitted. Shared atomic command
leasing prevents concurrent delivery of the same command during its lease.
`poll_in_progress` is reserved and is not emitted by P2.

Results are schema-validated as a whole before any completion. A schema-invalid
batch returns 400 `invalid_payload` without completing any command. Valid
results are then processed independently: identical retries count as accepted;
unknown, foreign, unleased, expired, or conflicting command results are skipped
and do not count toward `{accepted:n}`. Completion is not batch-transactional.
The prior per-command 404/409 result errors are superseded by this rule.

Invalid user command shapes return 400 `command_invalid` before enqueueing.
Package, pinned-version, and proxy-path checks still use the named 422 errors.
Every route in this slice returns 404 in production unless
`LEAF_DEVICE_BRIDGE_ENABLED=1`; non-production environments enable the routes.
The enrollment install command is exactly
`curl -fsSL <origin>/api/devices/agent/install.sh | LEAF_ENROLL_TOKEN=<token> sh`.
The installer supplies its validated HTTPS origin without another caller variable.

Device public origins are distinct from the website redirect host list.
`platform.leafdesign.ai` and `platform-staging.leafdesign.ai` use their own
HTTPS origins. The `studio.leafautomation.ai` door maps to
`https://platform.leafdesign.ai`, matching `deploy/studio-door/vercel.json`;
agents and relay clients receive the canonical platform origin, never the door.
For local/test use, `LEAF_DEVICE_PUBLIC_ORIGINS` adds comma-separated HTTPS
origins without replacing these mappings. Production ignores that variable.
Unknown hosts, duplicate host/forwarded-host/forwarded-proto headers, and
non-HTTPS requests return 400 `invalid_request`.

An enrollment token is 32 cryptographically random bytes encoded as unpadded
base64url (43 characters). Its TTL is 600 seconds. Studio stores only
`sha256(raw_token_bytes)`, never the token or an install command containing it.
Registration must atomically consume the token and bind its project and tenant
to the new device. The `public_key` is a raw 32-byte Ed25519 public key in
unpadded base64url, not PEM. A public key belongs to only one device.
The registration body cannot choose a tenant, project, or device ID.
`install_command` invokes the official installer with that one-use token;
it is displayed only to the owner and is never a queued command. Installers
must avoid persisting it in shell history or logs. This contract fixes no
installer download URL; packaging owns the official release location.

`server_time` is an RFC 3339 UTC timestamp. Every authenticated poll, including
a 204 response, refreshes the device's last-seen time. Agents send full
heartbeats at least every 30 seconds, even while paused. A device is offline
when no authenticated poll or heartbeat has arrived for 90 seconds. Revoked
status takes precedence over offline; paused status comes from the agent's
reported refusal. `free_mem_mb` is free memory in MiB; `uptime_s` is the
nonnegative integer number of seconds since agent start.

Studio assigns command IDs and timestamps. It never delivers a command after
`expires_at`; the agent checks expiry again before execution. A command's
expiry must be later than its issue time. Device delivery can repeat after a
lease expires; agents deduplicate by command ID and resend the saved result
without repeating a completed operation. Identical result retries count toward
`accepted`; a different terminal result for an existing ID returns 409.
The result batch is validated and accepted atomically. Neither query parameters
nor headers may override the signed device ID or a command's device binding.

### Relay behavior

Relay tokens are independently revocable, scoped to one tenant/project/device/
package, and contain a UUID token ID plus a random 32-byte secret:
`<token_id>.<base64url-secret>`. Only a secret hash is stored. The prefix gives
the owner the `token_id` needed by DELETE without adding a response field.
They remain valid until token or device revocation. The response's
`relay_origin` is the HTTPS Studio origin; `path_prefix` is
`/api/device-relay/<package>` with the actual package name substituted, never
a device ID or a template placeholder. Clients append a package API path.
The token is sent as `Authorization: Bearer <relay_token>` only to that origin.
The relay bearer token alone binds the device and tenant/project; the route
does not carry or accept a device ID. A token whose bound package differs from
the path's package returns 403 `relay_token_mismatch` before enqueueing.
`RelayTokenResponse` rejects unknown fields and has exactly the three mint
response fields. This public synthetic example uses a zero-byte secret, never
a credential accepted by a live server:

```relay-token-example
{
  "relay_token": "00000000-0000-4000-8000-000000000012.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "relay_origin": "https://studio.example.invalid",
  "path_prefix": "/api/device-relay/homeassistant"
}
```

Ops imports the package's OpenAPI document from the public
`GET /api/devices/relay-openapi/{package}.json` endpoint. It contains only the
catalog's allowed relay operations, with fixed package paths and no device-ID
templating, credentials, or tenant-specific content. For the same published
catalog/contract revision, every server task returns identical UTF-8 JSON
bytes. `X-Leaf-Document-Sha256` is the lowercase hexadecimal SHA-256 of those
exact response body bytes. The document's server URL uses the HTTPS Studio
origin and `/api/device-relay/<package>` prefix.

`ANY` is routing notation, not permission to use every HTTP verb. Only
GET, POST, PUT, PATCH, DELETE can reach `http.proxy`, and the catalog must
explicitly allow the method and path. All other verbs return 403
`relay_path_denied`. Studio authorizes before enqueueing; the agent independently
checks the same allowlist from its pinned catalog. A direct user `http.proxy`
command gets the same checks. HA-specific commands use fixed internal HA
endpoints and never accept an arbitrary host, port, or path.

Normalize the incoming URL path once before authorization and pass that exact
path to the agent. Strictly decode percent escapes once as UTF-8. Reject invalid
escapes, remaining percent signs (double encoding), NUL/control characters,
backslashes, `?`, `#`, dot segments (`.` or `..`), empty internal segments,
and paths beginning `//`. Require one leading slash. The agent repeats these
checks and rejects any encoded/noncanonical path. Never resolve an absolute URL
or follow upstream redirects. A prefix ending `/` permits descendants under
that prefix. A prefix without a trailing `/` permits that exact path and
descendants separated by `/`, never a partial segment such as `/api/statesX`.
Query values cannot alter path matching. `query` is a map of nonempty string
keys to string values; duplicate incoming query keys return 400
`invalid_request`. The agent URL-encodes that map as query parameters only.

Studio enqueues `http.proxy` and waits at most 8 seconds for its result. That
command expires at the relay deadline; a timed-out caller must not cause an
expired command to run later. Offline or revoked devices return 503
`device_offline` without enqueueing. Expiry of the wait returns 504
`device_timeout`. The agent resolves the package's loopback port from its own
catalog. It never forwards the relay bearer, caller cookies, Host, or arbitrary
caller headers to HA; it supplies the local HA credential itself.
Relay calls accept and drop an `Idempotency-Key` header: it is not forwarded,
included in command args, persisted, or used to deduplicate relay calls. Each
call creates its own command; command-ID retry deduplication still applies.

`http.proxy` success data is
`{status:<100..599 integer>, content_type:<JSON media type>, body_json:<JSON>}`.
Studio returns that status, the JSON content-type, and serialized JSON body;
it forwards no other upstream headers. Upstream HTTP errors with valid JSON
still use this success envelope so callers receive the upstream status.
A bodyless 204/304 is the only exception: omit `body_json`, forward no body,
and do not invent a JSON payload. Request and response bodies are at most
1 MiB (1,048,576 UTF-8 bytes). Requests with non-JSON bodies return 415;
oversize requests return 413. Non-JSON or oversized upstream responses,
connection failures, or malformed proxy results become 502 `upstream_error`.
No HTML, binary, streaming, or WebSocket traffic is supported.

## Request signing

Device-signed routes require these headers:

```text
X-Leaf-Device: <device_id>
X-Leaf-Timestamp: <unix seconds>
X-Leaf-Nonce: <16 random bytes base64url>
X-Leaf-Signature: <base64url Ed25519 signature>
```

The timestamp is a decimal integer in UTC seconds. The nonce is 22 unpadded
base64url characters; the signature encodes 64 bytes in 86 unpadded characters.
Hash the raw HTTP body bytes before parsing JSON. An absent body hashes the
empty byte string. Sign the UTF-8 encoding of the following six lines, joined
with a single LF byte (`\n`), with **no trailing newline**:

```text
LEAF-DEVICE-V1
<UPPERCASE method>
<path including query string>
<timestamp header verbatim>
<nonce header verbatim>
<lowercase hex SHA-256 of raw body>
```

Use the original origin-relative request target, including percent encoding,
query ordering, and a `?` when present. Do not decode or sort it for signing.
The server verifies against the device ID's stored public key. Check key
revocation before accepting work; then validate skew and signature, and atomically
claim the nonce in shared storage. Never consume a nonce from an invalid
signature. The server rejects absolute clock skew greater than 60 seconds with
401 `signature_stale`, a nonce already accepted for that device in the last
120 seconds with 401 `signature_replay`, and malformed/missing signing headers,
unknown device IDs, or bad signatures with 401 `signature_invalid`. Exact
60-second skew is allowed. A retry must use a fresh nonce and signature.
Revoked known devices receive 401 `device_revoked` on signed endpoints.

The following JSON fenced block is the shared golden vector. Its private key
is derived from **32 seed bytes, each 0x01**. It is public test material, never
a production credential. The signature was computed with `cryptography`
Ed25519 and must match byte for byte in both server and agent implementations.
`body` is the raw UTF-8 string, not a JSON serialization of the field itself.

```signing-example
{
  "seed_hex_byte": "01",
  "seed_length": 32,
  "device_id": "00000000-0000-4000-8000-000000000001",
  "public_key": "iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w",
  "method": "GET",
  "path": "/api/devices/agent/poll?wait=25",
  "timestamp": "1793613600",
  "nonce": "AAECAwQFBgcICQoLDA0ODw",
  "body": "",
  "body_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "signed_string": "LEAF-DEVICE-V1\nGET\n/api/devices/agent/poll?wait=25\n1793613600\nAAECAwQFBgcICQoLDA0ODw\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "signature": "B8qt6gc7SV0LsK9Ky5U9OBjA7AtoYvMszyu3f-pFdeYL-a_-A-fZFroOei0K1GpKVQ3fRYgbiX-e24KHEeqcBg"
}
```

## Commands

`Command` is `{command_id, op, args, issued_at, expires_at}`. All fields are
required. The operation table is the complete v1 allowlist. Argument fields
without `?` are required. Every argument object rejects unknown fields.

| Operation | Exact args | Behavior |
| --- | --- | --- |
| `pkg.install` | `{package, version}` | Install a known package at a pinned catalog version. |
| `pkg.upgrade` | `{package, version}` | Upgrade to a pinned catalog version. |
| `pkg.uninstall` | `{package, keep_data:bool}` | Remove a known package; retain its managed data only if requested. |
| `pkg.health` | `{package}` | Check the package's catalog health path. |
| `http.proxy` | `{package, method, path, query?, body_json?}` | Proxy only an allowed method/path to the package's catalog loopback port. |
| `ha.onboard` | `{}` | Run the fixed local HA onboarding procedure; credentials remain local. |
| `ha.flow.start` | `{handler, data?}` | Start an HA flow; handler is exactly `hue`, `wiz`, or `group`. |
| `ha.flow.step` | `{flow_id, data}` | Submit a step to that local HA flow. |
| `ha.flow.list` | `{}` | List local HA flows. |
| `agent.pause` | `{}` | Set the local kill switch and stop command execution. |
| `agent.update` | `{version, sha256}` | Reserved; agents MAY return `refused/unsupported_op`. |

`http.proxy.method` is GET, POST, PUT, PATCH, or DELETE. `path` is an absolute
package API path, not a filesystem path; query and body rules are above.
HA flow `data` is a JSON object. Flow IDs are nonempty strings of at most
128 characters; they refer only to local HA flows. HA operations implicitly
target the `homeassistant` package. No operation accepts raw shell text or
arbitrary file/port parameters. Package-managed paths and launch routines are
fixed in the agent. For install/upgrade, both package and version must appear
in its pinned catalog. An absent package returns `package_unknown`; an
unlisted version returns `version_unknown`.

## Results

`Result` is `{command_id, status, finished_at, data?, error?}`. Status is exactly
`ok`, `error`, or `refused`. `ok` has no `error`. Both `error` and `refused`
require `error:{code,message}`. `data` is JSON; it must not contain credentials.
The message is a safe nonempty explanation of at most 1024 characters.
`refused` means the agent rejected execution; `error` means attempted work
failed. Errors use the following closed code list:

| Code | Meaning |
| --- | --- |
| `unsupported_op` | Unknown operation or reserved operation not supported. |
| `package_unknown` | Package is absent from the pinned catalog. |
| `path_denied` | Proxy method/path fails the catalog allowlist or normalization. |
| `paused` | The local kill switch prevents execution. |
| `upstream_error` | Local HA/package connection or response failed. |
| `timeout` | Execution or local upstream deadline expired. |
| `install_failed` | Known package installation/upgrade/removal failed. |
| `command_expired` | Command expired before execution. |
| `invalid_args` | Arguments fail validation. |
| `version_unknown` | Release version is absent from the pinned catalog. |

The schema rejects unknown operations before queuing. Agents that receive an
unknown operation through a damaged or newer peer still return
`refused/unsupported_op`, not arbitrary execution.

## Package catalog

`PackageCatalogEntry` is
`{package, display_name, version, port, health_path, relay_allow:[{method,path_prefix}]}`.
All fields are required. Studio owns the catalog; agents keep a trusted pinned
copy. The port is an integer in 1..65535. Health and relay paths obey the same
canonical path rules as proxy paths. `health_path` grants only the agent a
local health check; it does not add a relay permission. `relay_allow` may be
empty. The entry below fixes HA's port and relay scope. Its version is an
illustrative pinned release, not a claim that a device has installed it.

```package-catalog-example
{
  "package": "homeassistant",
  "display_name": "Home Assistant",
  "version": "2026.10.0",
  "port": 8123,
  "health_path": "/api/",
  "relay_allow": [
    {"method": "GET", "path_prefix": "/api/states"},
    {"method": "POST", "path_prefix": "/api/services/light/"},
    {"method": "POST", "path_prefix": "/api/services/scene/"},
    {"method": "POST", "path_prefix": "/api/services/homeassistant/turn_off"},
    {"method": "POST", "path_prefix": "/api/services/homeassistant/turn_on"}
  ]
}
```

## Device and audit views

`DeviceView` has exactly these required fields:
`{device_id, project_id, label, hostname, platform:{os,os_version,arch}, agent_version, packages:[PackageState], status, enrolled_at, last_seen_at, revoked_at}`.
Status is `online`, `offline`, `paused`, or `revoked`. The last two timestamps
may be null when no observation/revocation exists. No private key, enrollment
token, relay token, HA token, or public key appears in this view.
`project_id` and `label` are nonempty strings of at most 128 characters;
`hostname` is 1..253 characters. Platform OS and architecture are nonempty
strings of at most 64 characters, OS version and agent version at most 128.
The first Mac agent reports `os:"darwin"` and `arch:"arm64"` where applicable.

`PackageState` is `{package, version, status, last_health_at}`, all required.
Status is `installed`, `running`, `stopped`, or `error`. `last_health_at` is
null until checked, otherwise a timestamp. An uninstalled package is absent
from the list. Heartbeat and view lists contain at most 64 package states,
one per package. Package and agent versions are at most 128 characters.

`AuditEvent` is `{at, actor, op, target, status, request_sha256?}`. Actor is
`user:<Auth0 sub>`, `device`, or `relay:<token_id>`. Status is `ok`, `error`,
or `refused`. `op` is the command operation or lifecycle action such as
`device.enroll`, `device.register`, `device.revoke`, `relay.create`, or
`relay.revoke`; `target` is the affected device, command, package, or token ID.
For a device actor, the enclosing audit route supplies its device identity.
The optional hash covers raw request bytes. Audit records contain **no bodies**,
credential values, signatures, HA flow data, relay query values, or response
bodies. A target must be an identifier, never a credential or raw request URL.

## Kill switch and revoke

The agent checks the kill-switch file before **every** command:

- macOS: `~/Library/Application Support/LeafAgent/DISABLED`
- Linux: `~/.local/share/leaf-agent/DISABLED`

If the file exists, every command, including package health, proxy, and update,
returns `refused` with error code `paused`. Polling and heartbeats continue;
the agent does not execute pending work. `agent.pause` creates this file and
returns `ok` for that transition. Subsequent commands return `refused/paused`.
Resuming requires the local owner to remove the file. There is no remote resume
operation. A remote command cannot choose or remove the kill-switch path.

Revocation is durable and takes effect across all ECS tasks. It invalidates
relay tokens, prevents new commands and relay requests, and stops delivery of
queued work. The next poll returns 401 `device_revoked`. On this response the
agent deletes its private key and exits non-zero. It must also do so when
another signed endpoint returns that code. An offline device learns revocation
only when it reconnects; already executing local work cannot be recalled.
Enrolling again requires a new enrollment token and a new device key.

## Versioning

The contract name is `leaf.device-bridge.v1`. Changes within v1 are additive
only. Do not remove fields, rename fields or operations, widen relay permissions,
or change signing bytes or existing field meanings. Because objects and enums
are closed, adding a field or operation requires coordinated schema upgrades
and capability support before a sender uses it. New required fields, incompatible
semantics, and signing changes require a new contract version. Unsupported
optional operations must fail safely. Keep the signing vector and fixtures
shared between server and agent tests; a live integration check belongs to
the later route/agent slices.

The JSON fixtures contain public synthetic values. Their all-`A` enrollment
token encodes 32 zero bytes to test the wire shape; it is never issued or accepted
by a live server. Production enrollments must use fresh random bytes.

## P3 relay and health amendment

An `ok` Result for `pkg.health` has `data` exactly equal to a `PackageState`.
`status: running` in that PackageState means the package is healthy.

The following P3 rules supersede earlier relay timing and size descriptions.
`POST /api/devices/{device_id}/relay-tokens` requires project ownership and
`link_service`, accepts exactly `{package, label}`, and requires that the
catalog package has relay permissions and appears in the device's package
states. An absent installed package returns 422 `package_not_installed`.
Its 201 response is `{token_id, relay_token, relay_origin, path_prefix}`;
only this response exposes the credential. `relay_origin` is the same
validated public HTTPS origin as enrollment. `path_prefix` is exactly
`/api/device-relay/<package>`; the relay path never contains a device ID.
The token alone selects its device and package.

`GET /api/devices/{device_id}/relay-tokens` requires project read access and
returns `{tokens:[{token_id, package, label, created_at, revoked_at,
last_used_at}]}`, including revoked entries but no secrets or secret hashes.
Deleting a known token through that device's token route is idempotent and
returns 204. Unknown, foreign-tenant, foreign-project, or foreign-device
token identifiers return 404. Mint/list/revoke actions are audited. Device
revocation invalidates all its relay tokens. Credential responses use
`Cache-Control: no-store`.

Relay requests require a single bearer authorization header. Invalid or
revoked tokens return 401 `relay_token_invalid`; a path package differing
from the token's package returns 403 `relay_token_mismatch`. The catalog's
method and normalized path allowlist applies, with segment-boundary prefix
matching. Dot segments, doubled slashes, backslashes, percent-encoded paths
(including encoded slashes), and query keys longer than 128 characters
return 403 `relay_path_denied`. Duplicate or empty query keys return 400
`invalid_request`. Query values never participate in path matching.

Bodies must be empty or valid JSON and at most 256 KiB (262,144 raw bytes).
Nonempty non-JSON media types return 415 `json_required`; malformed JSON
returns 400 `invalid_request`; oversized bodies return 413
`payload_too_large`. Relay tokens have an in-process bucket of 10 requests
per second and burst 20, keyed by token ID, with at most 10,000 LRU entries.
An exhausted bucket returns 429 `rate_limited`.

A device last seen 90 seconds ago or earlier returns 503 `device_offline`
without enqueueing. The server enqueues `http.proxy` with
`{package, method, path, query, body_json}` and TTL 15 seconds, then waits up
to 8 seconds, asynchronously polling every 100 ms. No result returns 504
`device_timeout`; the queued command subsequently expires at its 15-second
TTL. The request's `Idempotency-Key` is accepted and ignored. Caller headers,
including that key, cookies, host, and authorization, are never command
arguments. The agent supplies its own local credentials and JSON content type.

An `ok` proxy Result supplies `data.status` and `data.body`. P4's
`data.body_json` is also accepted for compatibility. The relay returns the
upstream status and serialized JSON with `Content-Type: application/json`;
204/304 responses have no body. Device headers are never returned. Malformed
or oversized upstream JSON (over 1 MiB serialized) becomes 502
`upstream_error`. An error/refused Result returns 502 `{error: <code>}`.
Other route errors retain the standard device error envelope.

Every authenticated relay call is audited with actor `relay:<token_id>`,
operation `http.proxy`, target `<METHOD> <path>` (bounded to the audit
schema's 256 characters), outcome `ok`, `error`, or `refused`, and the raw
request body's SHA-256. Rejected oversized streams hash the bytes consumed
before rejection. No bodies, query values, caller headers, or credentials
are included in the audit. Requests whose token cannot be resolved have no
authenticated actor. The production `LEAF_DEVICE_BRIDGE_ENABLED=1` gate
applies to token management, relay calls, and the public OpenAPI endpoint.

`GET /api/devices/relay-openapi/homeassistant.json` is public and returns
deterministic OpenAPI 3.0.3 JSON: sorted keys, compact separators, no
timestamps, `servers: [{url: <validated public origin>}]`, and HTTP bearer
security scheme `bearer`. `X-Leaf-Document-Sha256` hashes the exact response
bytes. At origin `https://platform.leafautomation.ai`, that digest is
`913a4329f910e6a8b768282dafb58594aa2c1f017ac7bdbb495705905fedd695`.
It exposes these JSON operations, each returning an array of loose state
objects on 200:

- `GET /api/device-relay/homeassistant/api/states`: `listStates`.
- `POST /api/device-relay/homeassistant/api/services/light/turn_on`:
  `lightTurnOn`, required `entity_id` (string or string array), optional
  `brightness_pct` 0..100, `rgb_color` three integer channels 0..255,
  `color_temp_kelvin` 2000..6500, and `transition` 0..60 seconds.
- `POST /api/device-relay/homeassistant/api/services/light/turn_off`:
  `lightTurnOff`, required `entity_id`, optional `transition`.
- `POST /api/device-relay/homeassistant/api/services/scene/turn_on`:
  `sceneTurnOn`, required `entity_id`.

The server Home Assistant catalog pin is `2026.10.4`.
