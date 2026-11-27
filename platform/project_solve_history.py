"""Display-safe proposal history for an already authorized drawing version."""

from .canonical_jobs import PROJECT_GRAPH_JOB_SCHEMA
from .db import connection


_PUBLIC_FAILURES = frozenset({
    "ATTEMPTS_EXHAUSTED",
    "CLOUD_AUTH_MISSING",
    "CLOUD_REQUEST_INVALID",
    "CLOUD_REQUEST_REJECTED",
    "CLOUD_RESPONSE_INVALID",
    "CLOUD_TENANT_UNAUTHORIZED",
    "CLOUD_UPSTREAM_FAILURE",
    "INVALID_FINAL_GRID_ORDER",
    "solve_outcome_unknown",
})


def latest_by_frame(binding, intake_sha256, graph_sha256, frame_ids):
    """Rank jobs after restricting them to the resolved drawing and its frames."""
    if not frame_ids:
        return {}
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (frame_id) frame_id, status,
                   error->>'reason_code' AS reason_code,
                   error->>'error_code' AS error_code
            FROM (
                SELECT execution_context->'solve_binding'->>'frame_id' AS frame_id,
                       status, error, created_at, job_id
                FROM jobs
                WHERE org_id = %(org)s AND project_id = %(project)s
                  AND input_version_id = %(version)s AND deleted_at IS NULL
                  AND kind = 'run' AND tool_name = 'solar-solve-proposal'
                  AND execution_context->>'schema' = %(schema)s
                  AND execution_context->>'authority_mode' = 'postgres_canonical'
                  AND execution_context->>'execution_path' = 'local'
                  AND execution_context->>'drawing_id' = %(drawing)s
                  AND execution_context->>'parent_intake_sha256' = %(intake)s
                  AND execution_context->'solve_binding'->>'graph_sha256' = %(graph)s
                  AND execution_context->'solve_binding'->>'frame_id' = ANY(%(frames)s)
            ) AS matching
            ORDER BY frame_id, created_at DESC, job_id DESC
            """,
            {"org": binding.organization_id, "project": binding.project_id,
             "version": binding.input_version_id, "drawing": str(binding.drawing_id),
             "intake": intake_sha256, "graph": graph_sha256, "frames": frame_ids,
             "schema": PROJECT_GRAPH_JOB_SCHEMA},
        )
        history = {}
        for row in cur:
            entry = {"status": row["status"]}
            if row["status"] == "failed":
                reason = row["reason_code"] or row["error_code"]
                entry["reason_code"] = reason if reason in _PUBLIC_FAILURES else "GRAPH_JOB_FAILED"
            history[row["frame_id"]] = entry
        return history
