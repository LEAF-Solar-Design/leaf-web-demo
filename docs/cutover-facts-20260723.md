# Leaf Platform PostgreSQL cutover facts

Captured read-only on 2026-07-23 from AWS account `807034087062`, production
`us-east-1`. The local CLI identity resolved to the account root, so no AWS
mutation was performed.

## ECS and load balancer

- Service: `leaf-automation-production/leaf-platform`
- Steady desired count: `1`
- Deployment configuration: minimum healthy `0`, maximum `100`
- Deployment circuit breaker and automatic rollback: disabled
- Target group: `leaf-platform/56e6b9f9ba782991`
- Deregistration delay: `300` seconds
- Health path: `/api/health`
- Target port: `8130`

During capture, GitHub Actions run identity `GHA-Deploy-30032531400` deployed
task definition revision 38. The service stopped revision 36 before starting
revision 38 because its deployment percentages prohibit overlap. The old target
then drained for about five minutes. Revision 38 failed to start when the broker
container exited with code 1. The workflow registered revision 39 with the prior
image digests. Revision 39 reached one running task and one healthy target. No
manual rollback was needed.

This event is direct evidence that the current `0/100` deployment policy and
300-second drain can create a material service gap. Do not change the policy
until every mutable authority has passed the two-writer gates.

## Current task topology

One ECS task contains four containers:

- `init-platform-data`
- `broker`
- `harness`
- `app`

The task definition mounts the same EFS volume at `/data` in all four
containers. Scaling the service therefore scales the app, broker, and harness
together. Broker and harness state are on the critical path for safe overlap.

## EFS

- File system: `fs-04d580c2e056df4c3`
- State: available
- Encryption: enabled with KMS
- Performance mode: general purpose
- Throughput mode: elastic
- Access points: none
- Task mount root: `/`
- Transit encryption: enabled

Shared EFS does not make process-memory, SQLite, JSON, or JSONL authorities safe
for multiple tasks.

## Observed traffic

CloudWatch data was available from 2026-07-21 through 2026-07-23:

- Total target-group requests in the available period: `12,871`
- Highest hourly request count: `3,697`
- Highest hourly completed-request p99: `0.2243` seconds

The p99 value does not establish a safe stream drain duration. Long-running
streaming requests must be measured separately before changing the 300-second
target-group delay.

## Read commands

Evidence came from these read-only APIs:

- `ecs describe-services`
- `ecs describe-task-definition`
- `ecs describe-tasks`
- `elbv2 describe-target-groups`
- `elbv2 describe-target-group-attributes`
- `elbv2 describe-target-health`
- `efs describe-file-systems`
- `efs describe-access-points`
- `cloudwatch get-metric-statistics`
- `cloudtrail lookup-events`
