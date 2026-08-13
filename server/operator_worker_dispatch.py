"""Operator worker dispatch (contract/OPERATOR.md Lane D). The capability-only
operator handler (routers/operator_worker.py) forwards a BOUNDED job here; this
module never executes the commands in the app process. It posts them over the
secret-gated app->harness hop to the operator worker route, which runs them in
the isolated, egress-locked disposable worker (OperatorWorkerManager).

Bounds are enforced HERE, server-side, so a caller cannot widen isolation:
- the workspace is always disposable;
- network is always DENIED (broad operator work runs with no egress);
- the principal subject is the server-attested operator subject, never client
  input;
- the timeout and command count are capped.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import requests

import broker_client
from operator_deps import OperatorContext

MAX_TIMEOUT_MS = 1_800_000
MAX_COMMANDS = 50


class OperatorWorkerError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _worker_url() -> str:
    # The SAME app->harness hop the broker uses; the operator worker route is
    # protected by the same caller-auth secrets.
    return broker_client.broker_url()


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
    the commands in this process; the isolated worker runs them."""
    payload = build_dispatch_payload(ctx, commands, repo, timeout_ms)
    headers = {**broker_client.broker_headers(), **broker_client.harness_headers()}
    try:
        resp = requests.post(
            f"{_worker_url()}/operator/worker/dispatch",
            json=payload, headers=headers,
            timeout=(timeout_ms or 120_000) / 1000 + 30)
    except requests.RequestException as exc:
        raise broker_client.BrokerUnreachable(str(exc)) from exc
    return resp.json()
