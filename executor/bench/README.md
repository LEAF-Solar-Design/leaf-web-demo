# Executor benchmark contract

This directory defines the measurable gate for a warm executor pool. It is a
contract, not a claim that an executor exists or meets these targets.

Run the local contract checks from the repository root:

```powershell
python executor/bench/validate.py
python -m unittest executor.bench.tests.test_validate
```

The machine-readable inputs are `scenario-manifest.json` and
`result-schema.json`. A benchmark runner writes a result document that conforms
to the schema and then runs `validate.py --result <path>`. The checked-in
fixture is synthetic test data. It is not live performance evidence and the
validator rejects it as an SLO pass.

## Measurement rules

Each result records its exact source SHA, environment, host shape, concurrency,
payload bytes, code digest, and whether the call was warm or cold. A run must
also record the raw successful latency samples in milliseconds. Percentiles use
the nearest-rank method: sort N samples, then select index
`max(ceil(p / 100 * N), 1) - 1`. Failed calls never enter latency percentiles.
They remain in `failures` and count against each scenario's failure allowance.

The runner must timestamp and emit these ordered preparation events:

1. `assignment_completed`
2. `code_validation_completed`
3. `code_loaded`
4. `first_call_started`

For every first call, preparation events 1 through 3 must occur before event 4.
The gate rejects any result that reports this invariant as false.

## Required scenario gate

| Scenario | Measure | Initial gate |
| --- | --- | --- |
| `prepare_before_first_call` | assignment, validation, and load complete before the first call | all ordered-event checks pass |
| `warm_invocation_startup` | executor dispatch to user-code entry | p95 under 10 ms |
| `trivial_cloud_rpc` | caller request to cloud response for a trivial tool | report p50/p95/p99, p99 under 100 ms; planning range 20 to 100 ms |
| `concurrent_sessions_per_host` | isolated warm sessions under stepped concurrency | pass each requested level with no isolation breach |
| `code_reload` | replace a code digest while sessions remain usable | new digest only, no stale execution, and recovery reported |
| `host_drain` | stop assignments, finish or hand off active sessions | no assignment after drain and all sessions have a terminal disposition |
| `batch_handoff` | heavy work leaves the instant path | return an accepted job ID, not an inline final result |

`zero_docker_starts` is an invariant for every instant scenario. The runner must
collect Docker or container runtime start counters immediately before and after
the measured window. Their delta must be zero. The same trace must prove that
Redis, S3, Docker/ECS, the broker, and the database have zero per-call spans or
operations for an instant call. Startup, pool fill, telemetry export, and batch
workers are not part of the measured instant window.

Proof requires both instrumentation and a negative test. Instrument every
dependency client at the instant-call boundary, tag spans with a request ID,
and emit `instant_path_dependencies`. The runner must execute a trivial instant
call while each listed dependency client is replaced with a fail-fast sentinel.
The call must still succeed, the counter delta must be zero, and the trace must
contain no matching dependency span. A result records the three proofs in
`instant_path_proof`. A mere lack of an error is not proof.

## Capacity and cost worksheet

These are planning constants, not measured host capacity. Start with eight
resident warm execution slots per host. Treat 70% active-slot occupancy as the
maximum sustained operating point, reserve 30% headroom for burst and recovery,
start scale-out or admission control at 85%, and stop new instant assignments at
95% until capacity recovers. Replace these values only after the concurrent
sessions scenario passes at the proposed host shape.

At every concurrency step, compare a noisy-neighbor control session with a
single-tenant baseline. Flag the host if control p95 rises by more than 20%, if
its error rate rises above the scenario allowance, or if one tenant consumes
more than 70% of active slots for five consecutive samples. Also record CPU,
memory, runtime event-loop delay, queue depth, throttling, and per-tenant slot
share. A capacity claim needs at least three repeated runs at the same source
SHA and host shape.

Run idle-timeout experiments at 5, 15, 30, and 60 minutes. At each timeout,
measure warm retention, the first call after idleness, warm-slot residency,
drain behavior, and the resulting host-hours. Select the shortest timeout that
meets the warm-start SLO and the agreed retention target. Do not infer a cost
from this repository.

Use the following worksheet with operator-supplied prices and observed host
hours. `730` is an explicit planning month assumption, not an AWS price.

```
warm_pool_monthly_cost =
  warm_hosts * host_price_per_hour * 730
  + runtime_fixed_monthly_cost
  + telemetry_monthly_cost
  + network_egress_gb * operator_supplied_egress_price_per_gb

cost_per_successful_instant_call =
  warm_pool_monthly_cost / successful_instant_calls_per_month
```

Keep heavy work on the separate batch contract. The instant endpoint returns
HTTP 202 with `{ "job_id": "...", "status": "accepted" }`. The client polls or
subscribes for the terminal batch result. It must not wait for, proxy, or return
the heavy result on the instant RPC.
