"""Operator worker dispatch (contract/OPERATOR.md Lane D). The capability-only
operator handler (routers/operator_worker.py) forwards a BOUNDED job here; this
module never executes the commands in the app process. It posts them over the
secret-gated app->harness hop (LEAF_OPERATOR_HARNESS_URL + X-Harness-Secret,
the same hop the operator turn forwarder uses) to the harness operator worker
route, which runs them in the isolated disposable worker (OperatorWorkerManager).

Bounds are enforced HERE, server-side, so a caller cannot widen isolation:
- the workspace is always disposable;
- network is always DENIED (broad operator work runs with no egress);
- the principal subject is the server-attested operator subject, never client
  input;
- the timeout and command count are capped.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

import requests

import broker_client
from operator_deps import OperatorContext

MAX_TIMEOUT_MS = 1_800_000
MAX_COMMANDS = 50


class OperatorWorkerError(RuntimeError):
    def __init__(self, reason: str, http_status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def _worker_url() -> str:
    # The app->HARNESS hop (the same one the operator turn forwarder in
    # routers/operator_sessions.py uses) - NOT the broker: the broker serves no
    # operator route. Fail closed when the harness is unconfigured.
    base = os.environ.get("LEAF_OPERATOR_HARNESS_URL", "").rstrip("/")
    if not base:
        raise OperatorWorkerError("harness_not_configured", http_status=503)
    return base


def build_dispatch_payload(ctx: OperatorContext, commands: List[str],
                           repo: Optional[str],
                           timeout_ms: Optional[int]) -> Dict[str, Any]:
    if not isinstance(commands, list) or not commands or len(commands) > MAX_COMMANDS:
        raise OperatorWorkerError("commands_invalid")
    return {
        # workspace + network are FIXED server-side; a client cannot widen them.
        "workspace": "disposable",
        "network": [],  # always fully denied
        "commands": commands,
        "repo": repo,
        "principalSubject": ctx.subject,   # server-attested, never client input
        "sessionId": f"op-{uuid.uuid4().hex[:12]}",
        "idempotencyKey": uuid.uuid4().hex,
        "timeoutMs": min(int(timeout_ms or 120_000), MAX_TIMEOUT_MS),
    }


def dispatch_to_isolated_worker(ctx: OperatorContext, commands: List[str],
                                repo: Optional[str] = None,
                                timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """POST the bounded job to the harness operator worker route. NEVER executes
    the commands in this process; the isolated worker runs them. A non-200 from
    the harness is a REFUSAL and raises - it is never returned as a success."""
    payload = build_dispatch_payload(ctx, commands, repo, timeout_ms)
    # Harness caller-auth only: this hop terminates at the harness, so the
    # broker secret has no business on it.
    headers = broker_client.harness_headers()
    # The connection timeout derives from the CAPPED payload value, never the
    # raw client value, so a client cannot hold an app thread past the cap.
    capped_timeout_ms = payload["timeoutMs"]
    try:
        resp = requests.post(
            f"{_worker_url()}/operator/worker/dispatch",
            json=payload, headers=headers,
            timeout=capped_timeout_ms / 1000 + 30)
    except requests.RequestException as exc:
        raise broker_client.BrokerUnreachable(str(exc)) from exc
    if resp.status_code != 200:
        code = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
        except ValueError:
            pass
        # Truthful mapping: isolation refusal and dark-ship are the service's
        # posture (503); anything else unexpected is a bad upstream hop (502).
        status = 503 if resp.status_code in (501, 503) else 502
        raise OperatorWorkerError(
            code or f"worker_http_{resp.status_code}", http_status=status)
    try:
        return resp.json()
    except ValueError as exc:
        raise OperatorWorkerError("worker_receipt_invalid", http_status=502) from exc
