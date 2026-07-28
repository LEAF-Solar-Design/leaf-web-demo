-- Durable singleton state for the protected EFS to PostgreSQL authority move.
-- The migrator changes `migrating` to `migrated` in the same SERIALIZABLE
-- transaction that inserts the four drawing-authority row categories.

CREATE TABLE IF NOT EXISTS drawing_authority_cutover (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    state TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    source_commit TEXT NOT NULL,
    run_id BIGINT NOT NULL,
    run_attempt INTEGER NOT NULL,
    task_definition_arn TEXT NOT NULL,
    source_task_definition_arn TEXT NOT NULL,
    efs_id TEXT NOT NULL,
    fence_path TEXT NOT NULL,
    source_counts JSONB NOT NULL,
    parity_digest TEXT,
    entered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline TIMESTAMPTZ NOT NULL,
    last_error TEXT,
    CONSTRAINT drawing_authority_cutover_singleton CHECK (id = 1),
    CONSTRAINT drawing_authority_cutover_state_allowed CHECK (state IN (
        'fence_closed', 'migrating', 'migrated', 'promoted', 'rolled_back'
    )),
    CONSTRAINT drawing_authority_cutover_schema_version_positive
        CHECK (schema_version >= 1),
    CONSTRAINT drawing_authority_cutover_source_commit_shape CHECK (
        source_commit ~ '^[0-9a-f]+$'
        AND length(source_commit) IN (40, 64)
    ),
    CONSTRAINT drawing_authority_cutover_run_id_positive CHECK (run_id >= 1),
    CONSTRAINT drawing_authority_cutover_run_attempt_positive CHECK (run_attempt >= 1),
    CONSTRAINT drawing_authority_cutover_task_definition_shape CHECK (
        task_definition_arn ~ '^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$'
    ),
    CONSTRAINT drawing_authority_cutover_source_task_definition_shape CHECK (
        source_task_definition_arn ~ '^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$'
    ),
    CONSTRAINT drawing_authority_cutover_efs_id_shape CHECK (efs_id ~ '^fs-[0-9a-f]+$'),
    CONSTRAINT drawing_authority_cutover_fence_exact
        CHECK (fence_path = '/data/state/drawing-mutations'),
    CONSTRAINT drawing_authority_cutover_source_counts_shape CHECK (
        jsonb_typeof(source_counts) = 'object'
        AND source_counts ?& ARRAY[
            'manifests', 'versions', 'attempts', 'purge_receipts'
        ]
    ),
    CONSTRAINT drawing_authority_cutover_parity_digest_shape CHECK (
        parity_digest IS NULL OR parity_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT drawing_authority_cutover_last_error_shape CHECK (
        last_error IS NULL OR last_error ~ '^[a-z0-9_]+$'
    ),
    CONSTRAINT drawing_authority_cutover_deadline_after_entry
        CHECK (deadline > entered_at),
    CONSTRAINT drawing_authority_cutover_migrated_has_digest CHECK (
        state NOT IN ('migrated', 'promoted') OR parity_digest IS NOT NULL
    ),
    CONSTRAINT drawing_authority_cutover_rollback_has_error
        CHECK (state <> 'rolled_back' OR last_error IS NOT NULL)
);
