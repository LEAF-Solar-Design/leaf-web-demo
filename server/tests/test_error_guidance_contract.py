from __future__ import annotations

from envelopes import ErrorCode, error_obj


RETRY_CLASSES = {"immediate", "backoff", "after_action", "never"}
ACTORS = {"user", "workspace_admin", "approver", "operator", "service"}


def test_every_error_code_has_actionable_guidance():
    for code in ErrorCode.ALL:
        error = error_obj(code, "Plain failure sentence.", retryable=False)
        assert error["error_code"] == code
        assert error["message"] == "Plain failure sentence."
        assert error["retryable"] is False
        assert error["retry_class"] in RETRY_CLASSES
        assert error["actor"] in ACTORS
        assert isinstance(error["next_action"], str)
        assert len(error["next_action"].split()) >= 2


def test_action_owner_and_next_step_match_common_user_failures():
    unauthenticated = error_obj(
        ErrorCode.UNAUTHENTICATED, "Sign-in required.", retryable=False)
    assert unauthenticated["actor"] == "user"
    assert unauthenticated["retry_class"] == "after_action"
    assert "sign in" in unauthenticated["next_action"].lower()

    entitled = error_obj(
        ErrorCode.ENTITLEMENT_REQUIRED, "Plan denied.", retryable=False)
    assert entitled["actor"] == "workspace_admin"
    assert entitled["retry_class"] == "after_action"
    assert "workspace" in entitled["next_action"].lower()

    transient = error_obj(
        ErrorCode.BROKER_UNREACHABLE, "Broker unavailable.", retryable=True)
    assert transient["actor"] == "service"
    assert transient["retry_class"] == "backoff"
    assert "retry" in transient["next_action"].lower()

    internal = error_obj(
        ErrorCode.INTERNAL, "Internal failure.", retryable=False)
    assert internal["actor"] == "operator"
    assert internal["retry_class"] == "never"
    assert "support" in internal["next_action"].lower()

