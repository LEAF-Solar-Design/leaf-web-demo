# Instant executor host addressing decision

Status: accepted for the trusted-platform staging tier

## Decision

Route each instant invocation to the assigned Fargate task's private IPv4
address. Do not put an ALB, NLB, Cloud Map service-level DNS name, Redis queue,
broker, or control-plane proxy in the invocation path.

Each executor task reads its private address from the ECS container metadata v4
endpoint at startup. It registers this endpoint with the control plane:

```text
https://<task-private-ip>:8088
```

The control plane stores that endpoint with the host ID and host epoch. The
opaque session assignment carries the endpoint to the authenticated harness.
The harness reuses an mTLS connection to that exact address for all calls bound
to the session.

## TLS identity

Connecting to an IP address must not disable certificate verification. All
executor server certificates in one environment contain the fixed DNS identity:

```text
executor.instant.internal
```

The control-plane and harness clients connect to the assigned private IP but set
TLS SNI and hostname verification to that fixed identity. The clients accept a
certificate only when it chains to the configured private CA and matches that
name. Configuration uses:

```text
LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME=executor.instant.internal
```

The staging trusted-platform tier may share one short-lived executor server
certificate across executor hosts. The private key is mounted only into the
supervisor container and is not present in the child environment or request.
Certificate rotation replaces tasks before the old certificate expires.

Host-to-control lifecycle calls use a different mTLS client identity and a
dedicated host lifecycle secret. The app control secret cannot register a host.
The signed lease still pins the host ID, slot ID, host epoch, slot epoch, claim,
session, digest, and lease sequence. A different task at a reused private IP
rejects the old lease because its local host identity and fencing state differ.

Unique workload identities such as SPIFFE are required before admitting hostile
tenant code. The shared staging server certificate is not that boundary.

## Endpoint discovery and validation

The executor supervisor, not user code, reads `ECS_CONTAINER_METADATA_URI_V4`.
It accepts one private IPv4 address from the task metadata response and combines
it with the configured HTTPS port. Local development may continue to use an
explicit loopback endpoint.

The control plane rejects a registered endpoint unless all conditions hold:

1. The scheme is HTTPS, except explicit loopback development.
2. The host is an IP address in the environment's configured private CIDR.
3. The port is the configured executor port.
4. The request has the executor lifecycle credential and valid mTLS client
   certificate.
5. The executor ID in the request matches the authorized lifecycle identity.

The initial staging CIDR is `10.20.0.0/16`. Production must use its canonical
VPC CIDR from Terraform and must not copy the staging value.

## Network policy

- Harness tasks may connect to executor tasks on TCP 8088.
- Control-plane tasks may connect to executor tasks on TCP 8088.
- Executor tasks may connect to the control plane on its private lifecycle port.
- Browsers, the public ALB, broker, batch workers, and unrelated services cannot
  connect to executor tasks.
- The executor application task role has no AWS permissions. The ECS execution
  role may pull the image, mount approved secrets, and write logs, but it is not
  exposed as an application task role.
- Redis and PostgreSQL are absent from the executor task and invocation path.

Fargate security groups apply to the whole task, not one child process. This
network design therefore remains trusted-platform only. A hostile-code runtime
must enforce its own capability and network boundary inside the host.

## Rejected options

### Cloud Map service DNS

Ordinary service discovery returns or load balances several task addresses. It
cannot guarantee that a lease for host A reaches host A. Cloud Map remains useful
for stable control-plane discovery, not host-bound invocation.

### ALB or NLB in front of the pool

A load balancer chooses a target independently of the signed slot assignment.
Per-host target groups would add high control complexity and fixed cost without
improving the direct-call SLO.

### Dynamic per-host private DNS and certificate issuance

This gives clean hostnames but requires a DNS control loop and per-task
certificate issuance before readiness. It adds more moving parts and can make
the warm-host startup path depend on Route 53 and a CA service. Revisit it when
unique workload identity is required.

### Dedicated in-memory RPC router

A credential-free router can multiplex persistent executor tunnels and remains
the fallback if direct private addressing fails at scale. It adds a service,
one network hop, connection state, and at least one more warm task. The staging
bench must falsify direct addressing before adding it.

## Acceptance tests

1. With two executor tasks, sessions assigned to different host IDs always reach
   the exact host in the signed lease.
2. TLS rejects a valid private IP with the wrong CA, missing client certificate,
   or wrong configured server name.
3. Killing a task stops new assignments within one heartbeat expiry interval.
4. Reusing its IP for a new task does not allow an old lease to execute.
5. A warm keep-alive call does not query DNS, Redis, PostgreSQL, or an AWS API.
6. The executor task's application credentials endpoint yields no task-role
   credentials because no task role is attached.
7. The real staging path keeps executor startup p95 below 10 ms and trivial tool
   response p95 below 100 ms.

If any of these tests fails, the decision returns to proposed state. The next
option to test is the dedicated credential-free RPC router, not an invocation
queue.
