-- Campaign lifetime funding and developer references. No default allocation.
CREATE UNIQUE INDEX IF NOT EXISTS campaigns_developer_scope ON campaigns(org_id,project_id,campaign_id);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_developer_scope ON campaign_tasks(org_id,project_id,campaign_id,task_id);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_developer_scope ON campaign_task_attempts(org_id,project_id,campaign_id,task_id,attempt_id,fence);
CREATE TABLE IF NOT EXISTS campaign_developer_allocations (
 org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL,
 allocation_id TEXT NOT NULL CHECK(char_length(allocation_id) BETWEEN 1 AND 128),
 limit_microusd BIGINT NOT NULL CHECK(limit_microusd BETWEEN 1 AND 1000000000000000),
 max_active INTEGER NOT NULL CHECK(max_active BETWEEN 1 AND 16),
 spent_microusd BIGINT NOT NULL DEFAULT 0 CHECK(spent_microusd>=0),
 reserved_microusd BIGINT NOT NULL DEFAULT 0 CHECK(reserved_microusd>=0),
 evidence_ref JSONB NOT NULL CHECK(jsonb_typeof(evidence_ref)='object' AND octet_length(evidence_ref::text)<=8192),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(org_id,project_id,campaign_id),
 FOREIGN KEY(org_id,project_id,campaign_id) REFERENCES campaigns(org_id,project_id,campaign_id),
 CHECK(spent_microusd+reserved_microusd<=limit_microusd)
);
CREATE TABLE IF NOT EXISTS campaign_developer_operations (
 operation_id TEXT PRIMARY KEY CHECK(operation_id ~ '^[0-9a-f]{64}$'),
 org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL,
 task_id UUID NOT NULL, parent_attempt_id UUID NOT NULL, parent_attempt_fence BIGINT NOT NULL CHECK(parent_attempt_fence>0),
 idempotency_key TEXT NOT NULL CHECK(idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
 request JSONB NOT NULL CHECK(jsonb_typeof(request)='object' AND octet_length(request::text)<=16384),
 current_sequence INTEGER NOT NULL DEFAULT 1 CHECK(current_sequence BETWEEN 1 AND 99999999),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(org_id,project_id,campaign_id,operation_id),
 UNIQUE(org_id,project_id,campaign_id,task_id),
 UNIQUE(org_id,project_id,campaign_id,idempotency_key),
 FOREIGN KEY(org_id,project_id,campaign_id) REFERENCES campaign_developer_allocations(org_id,project_id,campaign_id),
 FOREIGN KEY(org_id,project_id,campaign_id,task_id,parent_attempt_id,parent_attempt_fence) REFERENCES campaign_task_attempts(org_id,project_id,campaign_id,task_id,attempt_id,fence)
);
CREATE TABLE IF NOT EXISTS campaign_developer_reservations (
 org_id UUID NOT NULL, project_id UUID NOT NULL, campaign_id UUID NOT NULL, operation_id TEXT NOT NULL,
 sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 99999999),
 developer_attempt_id TEXT NOT NULL UNIQUE,
 expected_attempt_id TEXT,
 reservation_microusd BIGINT NOT NULL CHECK(reservation_microusd BETWEEN 1 AND 1000000000000000),
 admitted BOOLEAN NOT NULL DEFAULT FALSE,
 cost_microusd BIGINT CHECK(cost_microusd>=0 AND cost_microusd<=reservation_microusd),
 receipt_ref JSONB CHECK(jsonb_typeof(receipt_ref)='object' AND octet_length(receipt_ref::text)<=8192),
 resources_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), settled_at TIMESTAMPTZ,
 PRIMARY KEY(operation_id,sequence),
 FOREIGN KEY(org_id,project_id,campaign_id,operation_id) REFERENCES campaign_developer_operations(org_id,project_id,campaign_id,operation_id),
 CHECK(developer_attempt_id=left(operation_id,48)||'-'||lpad(sequence::text,8,'0')),
 CHECK((sequence=1 AND expected_attempt_id IS NULL) OR (sequence>1 AND expected_attempt_id=left(operation_id,48)||'-'||lpad((sequence-1)::text,8,'0'))),
 CHECK((cost_microusd IS NULL AND receipt_ref IS NULL AND NOT resources_reconciled AND settled_at IS NULL) OR
       (cost_microusd IS NOT NULL AND receipt_ref IS NOT NULL AND resources_reconciled AND settled_at IS NOT NULL))
);
