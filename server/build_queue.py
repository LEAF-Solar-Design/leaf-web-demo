"""
The build queue record, server side (standardization slice 11a).

A MIRROR of ``web/src/lib/buildQueue.js``: the same record shape, the same
three lane mappers (broker / fold / fleet), the same two-stage terminal rules,
the same bounds, and the same fail-closed validator. The two are pinned to one
another by ``contract/build-queue.v1.cases.json``: ``tests/test_build_queue.py``
runs every case through this module and ``web/src/lib/buildQueue.test.js`` runs
the same cases through the JS, so a rule changed on one side and not the other
fails a test on the side that did not move.

TWO-STAGE TERMINAL, never inferred. ``terminal.verified`` is true only when the
lane's OWN terminal artifact exists: a broker job's own terminal receipt (kind
``terminal``, written beside the completed job), a marathon milestone's
``verified_at`` under a real oracle, or a verification/gate-proof receipt on a
fleet task. A bare ``done``/``complete`` status word is NOT evidence on any
lane: ``validate_record`` refuses ``verified: true`` with no qualifying
receipt, the same way it already refuses ``promoted: true`` with none.
``terminal.promoted`` is true only when a PROMOTION artifact exists
(the prewarm cutover receipt ``leaf.staging-prewarm-relay.v1`` with something
dispatched, an App Store Connect result carrying a build id, a janitor
promotion stage), carried as a receipt of kind ``promotion``.

HARDENING CONTRACT. Every string is bounded (LIMITS), every number finite and
non-negative, every enum closed. ``validate_record`` raises BuildQueueError
naming the FIRST defect and never repairs; the mappers return ``None`` for a
row they cannot map honestly so the caller COUNTS the drop. Pure: no I/O, no
clock, no locale.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LANES = ("fold", "broker", "fleet")
STATES = ("queued", "running", "verifying", "done", "failed")
ACTIONS = ("cancel", "retry", "promote")
RECEIPT_KINDS = ("terminal", "verification", "promotion", "artifact", "gate-proof")
TINTS = ("ok", "warn", "err", "mut")
LIMITS = {"id": 128, "text": 200, "ref": 512, "receipts": 32, "records": 200}

PREWARM_RELAY_SCHEMA = "leaf.staging-prewarm-relay.v1"
APP_STORE_CONNECT_OK = ("succeeded", "processed", "valid", "accepted")
JANITOR_PROMOTED = ("promoted", "done", "complete")

_OPEN_STATES = frozenset({"queued", "running", "verifying"})


class BuildQueueError(ValueError):
    """A record that fails the wire contract. The message names the first defect."""


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #
def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _bounded(value: Any, limit: int) -> Optional[str]:
    """A non-empty str within ``limit`` chars, else None. Never trims."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value


def _clip(value: Any, limit: int = LIMITS["text"]) -> Optional[str]:
    """Text a person reads: clipped with an ellipsis (a presentation bound)."""
    if not isinstance(value, str) or not value:
        return None
    return value[: limit - 1] + "…" if len(value) > limit else value


def _non_negative(value: Any) -> Optional[float]:
    if not _is_num(value) or value < 0:
        return None
    return value


def _non_negative_int(value: Any) -> Optional[int]:
    n = _non_negative(value)
    if n is None:
        return None
    # JS Math.round rounds .5 up; Python's round() is banker's. Match JS.
    return int(math.floor(n + 0.5))


def _num_out(value: Optional[float]) -> Any:
    """Integral floats serialise as ints so the JSON matches the JS mirror."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def to_epoch_ms(value: Any) -> Optional[int]:
    """Epoch ms from epoch seconds (< 1e12), epoch ms, or an ISO-8601 string."""
    if _is_num(value):
        if value <= 0:
            return None
        return int(math.floor((value * 1000 if value < 1e12 else value) + 0.5))
    if isinstance(value, str) and 0 < len(value) <= 64:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        ms = int(math.floor(parsed.timestamp() * 1000 + 0.5))
        return ms if ms > 0 else None
    return None


# --------------------------------------------------------------------------- #
# JobRail's vocabulary (web/src/components/JobRail.jsx, moved to the JS lib)
# --------------------------------------------------------------------------- #
def is_quota_rejection(job: Dict[str, Any]) -> bool:
    error = job.get("error")
    code = error.get("error_code") if isinstance(error, dict) else None
    if code is None:
        code = job.get("error_code")
    return job.get("status") == "failed" and code == "quota_exceeded"


def is_entitlement_rejection(job: Dict[str, Any]) -> bool:
    return job.get("status") == "failed" and bool(job.get("entitlement_required"))


def format_cost_usd(value: Any) -> Optional[str]:
    """JobRail's costUsd text: 4 decimals under a cent, 2 above, None at zero."""
    if not _is_num(value) or value <= 0:
        return None
    return f"${value:.4f}" if value < 0.01 else f"${value:.2f}"


def broker_cost_usd(job: Dict[str, Any]) -> Optional[float]:
    cost = job.get("cost")
    if not isinstance(cost, dict):
        result = job.get("result")
        cost = result.get("cost") if isinstance(result, dict) else None
    usd = cost.get("usd_est") if isinstance(cost, dict) else None
    if isinstance(usd, str):
        try:
            usd = float(usd)
        except ValueError:
            return None
    return usd if _is_num(usd) and usd > 0 else None


def broker_state_tag(job: Dict[str, Any]) -> Dict[str, str]:
    status = job.get("status")
    if status == "running":
        return {"tint": "ok", "label": "running"}
    if status == "submitted":
        return {"tint": "mut", "label": "submitted"}
    if status == "failed":
        if is_quota_rejection(job):
            return {"tint": "warn", "label": "spend cap"}
        if is_entitlement_rejection(job):
            return {"tint": "warn", "label": "plan"}
        return {"tint": "err", "label": "failed"}
    if status == "complete":
        if job.get("degraded_mode"):
            return {"tint": "warn", "label": "degraded"}
        return {"tint": "ok", "label": "complete"}
    return {"tint": "mut", "label": status if isinstance(status, str) and status else "pending"}


# --------------------------------------------------------------------------- #
# receipts and promotion artifacts
# --------------------------------------------------------------------------- #
def parse_receipt(value: Any) -> Optional[Dict[str, Any]]:
    if not _is_obj(value):
        return None
    kind = _bounded(value.get("kind"), LIMITS["text"])
    if kind is None or kind not in RECEIPT_KINDS:
        return None
    ref = _bounded(value.get("ref"), LIMITS["ref"])
    if ref is None:
        return None
    raw_at = value.get("at")
    at = None if raw_at is None else to_epoch_ms(raw_at)
    if raw_at is not None and at is None:
        return None
    return {"kind": kind, "ref": ref, "at": at}


def _receipts_of(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        parsed = parse_receipt(item)
        if parsed is not None:
            out.append(parsed)
        if len(out) >= LIMITS["receipts"]:
            break
    return out


def promotion_receipt(value: Any) -> Optional[Dict[str, Any]]:
    """The promotion receipt a source row carries, or None (see the JS twin)."""
    if not _is_obj(value):
        return None
    relay = value.get("prewarm_relay")
    if _is_obj(relay) and relay.get("schema") == PREWARM_RELAY_SCHEMA:
        dispatched = relay.get("dispatched")
        count = len(dispatched) if isinstance(dispatched, list) else 0
        run_id = relay.get("relay_run_id")
        run_id = "" if run_id is None else str(run_id)
        run_id = _bounded(run_id, LIMITS["ref"])
        if count > 0 and run_id:
            return {"kind": "promotion", "ref": f"{PREWARM_RELAY_SCHEMA}#{run_id}", "at": to_epoch_ms(relay.get("at"))}
        return None
    asc = value.get("app_store_connect_result")
    if _is_obj(asc):
        status = asc.get("status")
        status = status.lower() if isinstance(status, str) else ""
        build_id = asc.get("build_id")
        build_id = _bounded("" if build_id is None else str(build_id), LIMITS["ref"])
        if status in APP_STORE_CONNECT_OK and build_id:
            return {"kind": "promotion", "ref": f"app_store_connect#{build_id}", "at": to_epoch_ms(asc.get("at"))}
        return None
    stage = value.get("promotion_stage")
    if _is_obj(stage):
        status = stage.get("status")
        status = status.lower() if isinstance(status, str) else ""
        ref = _bounded(stage.get("ref"), LIMITS["ref"])
        if status in JANITOR_PROMOTED and ref:
            return {"kind": "promotion", "ref": ref, "at": to_epoch_ms(stage.get("at"))}
        return None
    return None


def _with_promotion(receipts: List[Dict[str, Any]], source: Any) -> List[Dict[str, Any]]:
    promo = promotion_receipt(source)
    if promo is None:
        return receipts
    if any(r["kind"] == "promotion" and r["ref"] == promo["ref"] for r in receipts):
        return receipts
    if len(receipts) >= LIMITS["receipts"]:
        return receipts
    return receipts + [promo]


def _has_promotion(receipts: List[Dict[str, Any]]) -> bool:
    return any(r["kind"] == "promotion" for r in receipts)


def _has_verification(receipts: List[Dict[str, Any]]) -> bool:
    return any(r["kind"] in ("verification", "gate-proof") for r in receipts)


def _has_terminal_evidence(receipts: List[Dict[str, Any]]) -> bool:
    """Any receipt kind that counts as terminal evidence: a broker job's own
    terminal receipt, or a verification/gate-proof receipt on any lane."""
    return any(r["kind"] in ("terminal", "verification", "gate-proof") for r in receipts)


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #
def _record(**fields: Any) -> Dict[str, Any]:
    fields["started"] = _num_out(fields["started"])
    fields["elapsed_ms"] = _num_out(fields["elapsed_ms"])
    fields["estimate_ms"] = _num_out(fields["estimate_ms"])
    fields["cost_usd"] = _num_out(fields["cost_usd"])
    return {
        "id": fields["id"],
        "lane": fields["lane"],
        "state": fields["state"],
        "title": fields["title"],
        "requested_by": fields["requested_by"],
        "started": fields["started"],
        "elapsed_ms": fields["elapsed_ms"],
        "estimate_ms": fields["estimate_ms"],
        "cost_usd": fields["cost_usd"],
        "receipts": [dict(r) for r in fields["receipts"]],
        "terminal": {"verified": bool(fields["verified"]), "promoted": bool(fields["promoted"])},
        "actions": list(fields["actions"]),
        "status": {"word": fields["word"], "tint": fields["tint"], "detail": fields["detail"]},
    }


def validate_record(value: Any) -> Dict[str, Any]:
    """The wire validator. Raises BuildQueueError naming the first defect."""
    def fail(reason: str) -> BuildQueueError:
        return BuildQueueError(reason)

    if not _is_obj(value):
        raise fail("record: not an object")
    rec_id = _bounded(value.get("id"), LIMITS["id"])
    if rec_id is None:
        raise fail("id: missing or over bound")
    if value.get("lane") not in LANES:
        raise fail("lane: not one of fold | broker | fleet")
    if value.get("state") not in STATES:
        raise fail("state: not one of queued | running | verifying | done | failed")
    title = _bounded(value.get("title"), LIMITS["text"])
    if title is None:
        raise fail("title: missing or over bound")
    requested_by = None
    if value.get("requested_by") is not None:
        requested_by = _bounded(value.get("requested_by"), LIMITS["text"])
        if requested_by is None:
            raise fail("requested_by: not a bounded string")
    started = None
    if value.get("started") is not None:
        started = to_epoch_ms(value.get("started"))
        if started is None:
            raise fail("started: not a timestamp")
    elapsed = None
    if value.get("elapsed_ms") is not None:
        elapsed = _non_negative_int(value.get("elapsed_ms"))
        if elapsed is None:
            raise fail("elapsed_ms: not a non-negative integer")
    estimate = None
    if value.get("estimate_ms") is not None:
        estimate = _non_negative_int(value.get("estimate_ms"))
        if not estimate:
            raise fail("estimate_ms: not a positive integer")
    cost = None
    if value.get("cost_usd") is not None:
        cost = _non_negative(value.get("cost_usd"))
        if cost is None:
            raise fail("cost_usd: not a non-negative number")
    raw_receipts = value.get("receipts")
    if not isinstance(raw_receipts, list):
        raise fail("receipts: not an array")
    if len(raw_receipts) > LIMITS["receipts"]:
        raise fail(f"receipts: more than {LIMITS['receipts']}")
    receipts: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_receipts):
        parsed = parse_receipt(item)
        if parsed is None:
            raise fail(f"receipts[{index}]: malformed (kind must be one of {' | '.join(RECEIPT_KINDS)}, "
                       "ref a bounded string, at a timestamp or null)")
        receipts.append(parsed)
    terminal = value.get("terminal")
    if not _is_obj(terminal):
        raise fail("terminal: not an object")
    if not isinstance(terminal.get("verified"), bool):
        raise fail("terminal.verified: not a boolean")
    if not isinstance(terminal.get("promoted"), bool):
        raise fail("terminal.promoted: not a boolean")
    if terminal["promoted"] and not _has_promotion(receipts):
        raise fail("terminal.promoted: true without a promotion receipt")
    if terminal["verified"] and not _has_terminal_evidence(receipts):
        raise fail("terminal.verified: true without a terminal, verification or gate-proof receipt")
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list):
        raise fail("actions: not an array")
    if len(raw_actions) > len(ACTIONS):
        raise fail("actions: more than the declared verbs")
    actions: List[str] = []
    for verb in raw_actions:
        if verb not in ACTIONS:
            raise fail("actions: not one of cancel | retry | promote")
        if verb in actions:
            raise fail("actions: duplicate verb")
        actions.append(verb)
    status = value.get("status")
    if not _is_obj(status):
        raise fail("status: not an object")
    word = _bounded(status.get("word"), LIMITS["text"])
    if word is None:
        raise fail("status.word: missing or over bound")
    if status.get("tint") not in TINTS:
        raise fail("status.tint: not one of ok | warn | err | mut")
    detail = None
    if status.get("detail") is not None:
        detail = _bounded(status.get("detail"), LIMITS["text"])
        if detail is None:
            raise fail("status.detail: not a bounded string")
    return _record(
        id=rec_id, lane=value["lane"], state=value["state"], title=title,
        requested_by=requested_by, started=started, elapsed_ms=elapsed,
        estimate_ms=estimate, cost_usd=cost, receipts=receipts,
        verified=terminal["verified"], promoted=terminal["promoted"],
        actions=actions, word=word, tint=status["tint"], detail=detail,
    )


# --------------------------------------------------------------------------- #
# lane mappers
# --------------------------------------------------------------------------- #
def from_broker_job(job: Any, session_id: str = "this-session") -> Optional[Dict[str, Any]]:
    """A jobs-store record (routers/jobs.py ``_record_body`` shape) -> record."""
    if not _is_obj(job):
        return None
    raw_id = job.get("job_id")
    rec_id = _bounded(session_id if raw_id is None else str(raw_id), LIMITS["id"])
    tool = job.get("tool")
    title = _clip(tool if isinstance(tool, str) else None)
    if rec_id is None or title is None:
        return None
    tag = broker_state_tag(job)
    status = job.get("status")
    if status == "running":
        state = "running"
    elif status in ("submitted", "queued"):
        state = "queued"
    elif status == "complete":
        state = "done"
    elif status == "failed":
        state = "failed"
    else:
        return None
    cost = broker_cost_usd(job) if state == "done" else None
    detail: Optional[str] = None
    if state == "failed":
        error = job.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("error_code") or None
    elif state == "running":
        progress = job.get("progress")
        if progress and progress != "running":
            detail = progress
    elif state == "done" and job.get("degraded_mode"):
        detail = "local fallback"
    elif cost is not None:
        detail = format_cost_usd(cost)
    receipts = _with_promotion(_receipts_of(job.get("receipts")), job)
    if state in ("queued", "running"):
        actions = ["cancel"]
    elif state == "failed":
        actions = ["retry"]
    else:
        actions = []
    requested_by = job.get("requested_by")
    # A complete job is verified ONLY when its own terminal receipt (or a
    # verification/gate-proof receipt) is present. `state == "done"` alone is
    # the status word the route already knows can outrun the receipt (a
    # missing, oversized or digest-mismatched file reads as absent), so it is
    # never enough on its own — including for a degraded_mode completion.
    verified = state == "done" and _has_terminal_evidence(receipts)
    return _record(
        id=rec_id, lane="broker", state=state, title=title,
        requested_by=_clip(requested_by if isinstance(requested_by, str) else None),
        started=to_epoch_ms(job.get("created_at")),
        elapsed_ms=_non_negative_int(job.get("elapsed_ms")),
        estimate_ms=None, cost_usd=cost, receipts=receipts,
        verified=verified, promoted=_has_promotion(receipts),
        actions=actions, word=tag["label"], tint=tag["tint"],
        detail=_clip(detail if isinstance(detail, str) else None),
    )


def from_fold_state(state: Any, meta: Any = None) -> Optional[Dict[str, Any]]:
    """A multi-round run's ``state.json`` mapping plus what sits beside it."""
    meta = {} if meta is None else meta
    if not _is_obj(state) or not _is_obj(meta):
        return None
    run_id = state.get("run_id")
    if run_id is None:
        run_id = meta.get("run_id")
    run_id = _bounded(run_id, LIMITS["id"])
    if run_id is None:
        return None
    milestones = state.get("milestones")
    rows = list(milestones.values()) if _is_obj(milestones) else []
    total = len(rows)
    done = 0
    verified_at: Optional[str] = None
    last_error: Optional[str] = None
    for m in rows:
        if not _is_obj(m):
            continue
        if m.get("status") == "done":
            done += 1
        v = m.get("verified_at")
        if isinstance(v, str) and v and to_epoch_ms(v) is not None:
            verified_at = v if verified_at is None or v > verified_at else verified_at
        e = m.get("last_error")
        if isinstance(e, str) and e:
            last_error = e
    rounds = _non_negative_int(state.get("rounds")) or 0
    escalated_reason = state.get("escalated")
    escalated = isinstance(escalated_reason, str) and bool(escalated_reason)
    vacuous = state.get("mission_complete_vacuous") is True
    complete = state.get("mission_complete") is True and not vacuous
    unverified = state.get("unverified_complete") is True
    rip = state.get("round_in_progress")
    in_round = _is_obj(rip)
    progress = f"{done}/{total} milestones" if total else None

    verified = False
    if escalated:
        state_word, word, tint, detail, actions = "failed", "escalated", "err", _clip(escalated_reason), ["retry"]
    elif complete:
        verified = verified_at is not None
        if verified:
            state_word, word, tint, detail, actions = "done", "verified", "ok", progress, []
        else:
            state_word, word, tint, detail, actions = ("verifying", "unverified", "warn",
                                                       "complete without a milestone verification", ["retry"])
    elif vacuous:
        state_word, word, tint, detail, actions = "verifying", "unverified", "warn", "completed with no oracle", ["retry"]
    elif unverified:
        state_word, word, tint, actions = "failed", "unverified", "warn", ["retry"]
        detail = _clip(last_error) if last_error else "stopped before the oracle passed"
    elif in_round or rounds > 0:
        round_no = _non_negative_int(rip.get("round")) if in_round else None
        state_word, tint, detail, actions = "running", "ok", progress, ["cancel"]
        word = f"round {round_no if round_no is not None else rounds}"
    else:
        state_word, word, tint, detail, actions = "queued", "queued", "mut", progress, ["cancel"]

    receipts = _receipts_of(meta.get("receipts"))
    if verified and not any(r["kind"] == "verification" for r in receipts) and len(receipts) < LIMITS["receipts"]:
        receipts = receipts + [{"kind": "verification", "ref": f"{run_id}#verified_at", "at": to_epoch_ms(verified_at)}]
    receipts = _with_promotion(receipts, meta)
    promoted = _has_promotion(receipts)
    if state_word == "done" and verified and not promoted:
        actions = ["promote"]
    started = to_epoch_ms(meta.get("started_at"))
    mtime = to_epoch_ms(meta.get("state_mtime"))
    elapsed = mtime - started if started is not None and mtime is not None and mtime >= started else None
    title = meta.get("title")
    requested_by = meta.get("requested_by")
    return _record(
        id=run_id, lane="fold", state=state_word,
        title=_clip(title if isinstance(title, str) else run_id),
        requested_by=_clip(requested_by if isinstance(requested_by, str) else None),
        started=started, elapsed_ms=elapsed, estimate_ms=None,
        cost_usd=_non_negative(state.get("spent_usd")), receipts=receipts,
        verified=verified, promoted=promoted, actions=actions,
        word=word, tint=tint, detail=detail,
    )


_FLEET_STATES = {
    "active": ("running", "active", "ok"),
    "idle": ("running", "idle", "mut"),
    "waiting_human": ("running", "waiting on a person", "warn"),
    "blocked": ("running", "blocked", "warn"),
    "stalled": ("running", "stalled", "warn"),
    "queued": ("queued", "queued", "mut"),
    "complete": ("done", "complete", "ok"),
    "failed": ("failed", "failed", "err"),
    "abandoned": ("failed", "abandoned", "err"),
}


def from_fleet_task(row: Any) -> Optional[Dict[str, Any]]:
    """One collector row (task_state joined to tasks) -> record."""
    if not _is_obj(row):
        return None
    task_id = row.get("task_id")
    rec_id = _bounded(task_id if isinstance(task_id, str) else None, LIMITS["id"])
    if rec_id is None:
        return None
    raw_state = row.get("state")
    mapped = _FLEET_STATES.get(raw_state) if isinstance(raw_state, str) else None
    if mapped is None:
        return None
    state, word, tint = mapped
    receipts = _with_promotion(_receipts_of(row.get("receipts")), row)
    verified = state == "done" and _has_verification(receipts)
    promoted = _has_promotion(receipts)
    if state in ("queued", "running"):
        actions = ["cancel"]
    elif state == "failed":
        actions = ["retry"]
    else:
        actions = ["promote"] if verified and not promoted else []
    started = to_epoch_ms(row.get("created_at"))
    last = to_epoch_ms(row.get("last_evidence_at"))
    elapsed = last - started if started is not None and last is not None and last >= started else None
    # The gateway's own stamp only (slice 11b), or null: `owner` names who is
    # EXECUTING the task, never who asked for it.
    requested_by = row.get("requested_by")
    if not isinstance(requested_by, str):
        requested_by = None
    title = row.get("title")
    estimate = _non_negative_int(row.get("estimate_ms"))
    detail = row.get("detail")
    return _record(
        id=rec_id, lane="fleet", state=state,
        title=_clip(title if isinstance(title, str) and title else rec_id),
        requested_by=_clip(requested_by),
        started=started, elapsed_ms=elapsed,
        estimate_ms=estimate if estimate else None,
        cost_usd=_non_negative(row.get("cost_usd")), receipts=receipts,
        verified=verified, promoted=promoted, actions=actions,
        word=word, tint=tint, detail=_clip(detail if isinstance(detail, str) else None),
    )


def running_count(records: List[Dict[str, Any]]) -> int:
    return sum(1 for r in records if isinstance(r, dict) and r.get("state") in _OPEN_STATES)
