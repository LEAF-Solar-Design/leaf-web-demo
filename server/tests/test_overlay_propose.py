"""T1 chat entry point.

This module is why the T1 spine stopped being dead code: before it, nothing
called the registry, the store, or the stream. Each test names the failure it
prevents.

Run:  cd server && python -m pytest tests/test_overlay_propose.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import overlay_propose as op  # noqa: E402


class FakeStore:
    """Records what reached the storage boundary. A fake, not a mock: the point
    is to assert on the ARGUMENTS, which is what the real store validates."""

    def __init__(self):
        self.calls = []

    def create_proposal(self, **kw):
        self.calls.append(kw)
        return {**kw, "state": "pending", "revision": 0,
                "lease_expires_at": "2026-08-04T12:00:00Z"}


def _propose(**over):
    kw = dict(tenant_id="t1", session_id="s1",
              requested_tokens={"color.canvas.bg": "#ffffff"},
              store=FakeStore())
    kw.update(over)
    return op.propose(**kw)


# --------------------------------------------------------------------------- #
# It actually reaches the store
# --------------------------------------------------------------------------- #
def test_a_valid_request_opens_a_pending_preview():
    store = FakeStore()
    out = _propose(store=store)
    assert len(store.calls) == 1
    assert store.calls[0]["tenant_id"] == "t1"
    assert store.calls[0]["session_id"] == "s1"
    assert out["proposal"]["state"] == "pending"


def test_the_preview_is_scoped_to_ONE_session():
    """A proposal with no session would be a preview nobody is looking at, and
    the operator card could not say who asked."""
    for missing in ({"session_id": ""}, {"tenant_id": ""}):
        with pytest.raises(op.OverlayProposeError) as e:
            _propose(**missing)
        assert e.value.code == "session_required"


def test_a_lease_is_always_set():
    """Expiry is what clears an abandoned request without anyone sweeping."""
    store = FakeStore()
    _propose(store=store)
    assert store.calls[0]["lease_s"] == op.DEFAULT_LEASE_S


# --------------------------------------------------------------------------- #
# It delegates validation rather than re-implementing it
# --------------------------------------------------------------------------- #
def test_an_unknown_token_id_never_reaches_the_store():
    store = FakeStore()
    with pytest.raises(op.OverlayProposeError) as e:
        _propose(store=store, requested_tokens={"color.not.a.token": "#ffffff"})
    assert e.value.code == "invalid_tokens"
    assert store.calls == [], "an invalid request was persisted"


def test_a_hostile_value_never_reaches_the_store():
    """The exact reproduction from the review: url() in a chat request."""
    store = FakeStore()
    with pytest.raises(op.OverlayProposeError):
        _propose(store=store,
                 requested_tokens={"color.canvas.bg": "url(https://attacker.example/x)"})
    assert store.calls == []


def test_an_empty_request_is_refused():
    """An empty proposal puts a card in front of the operator with nothing to
    decide."""
    with pytest.raises(op.OverlayProposeError) as e:
        _propose(requested_tokens={})
    assert e.value.code in ("no_tokens", "invalid_tokens")


# --------------------------------------------------------------------------- #
# The announcement
# --------------------------------------------------------------------------- #
def test_no_event_is_emitted_when_the_stream_is_not_wired():
    """A proposal that exists but was not announced is recoverable: the next
    session read finds it. One announced but not stored is not."""
    assert _propose()["event"] is None


def test_the_proposed_event_carries_the_cas_witness():
    seen = {}
    out = _propose(
        current_document={"version": 7, "tokens": {}},
        append_event=lambda *a: 11,
        broadcast=lambda e: seen.update(e))
    assert out["event"]["data"]["document_version"] == 7
    assert out["event"]["seq"] == 11, "the broadcast must carry the durable seq"
    assert seen["type"] == "overlay_proposed"


def test_the_event_carries_token_IDS_not_values():
    out = _propose(append_event=lambda *a: 1, broadcast=lambda e: None)
    assert out["event"]["data"]["token_ids"] == ["color.canvas.bg"]
    assert "#ffffff" not in repr(out["event"]["data"])


def test_a_store_failure_means_no_event():
    """Announcing a proposal that was never stored would leave every client
    waiting on a preview that does not exist."""
    class Boom:
        def create_proposal(self, **kw):
            raise RuntimeError("unique violation")

    with pytest.raises(RuntimeError):
        _propose(store=Boom(), append_event=lambda *a: 1)


def test_request_text_is_carried_but_bounded():
    """Echoed for the operator card; bounded so a long paste cannot become the
    payload."""
    out = _propose(request_text="x" * 5000)
    assert len(out["request_text"]) == 500


# --------------------------------------------------------------------------- #
# The router's store resolution, under the condition that broke it in prod
# --------------------------------------------------------------------------- #
def test_store_resolves_when_the_stdlib_platform_module_wins_the_name():
    """`from platform import overlay_store` returned the STDLIB platform module
    in the container, so GET /api/overlay answered 500 on every request while
    this suite stayed green. Reproduce that condition here, then require
    _store() to hand back the platform package's overlay_store anyway.

    THE PRECONDITION IS EXECUTED, NOT DESCRIBED, AND MUST STAY THAT WAY. This
    test only means something where the pre-fix implementation would actually
    fail, so it establishes that by running the pre-fix expression itself and
    requiring it to raise. Do not "simplify" this into a check on the ambient
    `platform` module. Every such check admits an environment where the old
    implementation passes:

      `not hasattr(platform, "overlay_store")`  — a freshly imported REPO
          package lacks the attribute too, and the old import then loads the
          submodule and succeeds.
      no `__path__` plus `python_implementation` (i.e. "it is the stdlib") —
          holds even if something attached an `overlay_store` attribute to the
          real stdlib module.
      both of the above together — holds even so when
          `sys.modules["platform.overlay_store"]` exists, because IMPORT_FROM
          falls back to the qualified sys.modules entry when the parent has no
          such attribute.

    Executing the expression cannot be an incomplete description of the
    environment, because it is not a description. It is the condition.
    """
    # Import the module under test FIRST. Importing it can itself mutate import
    # state, so probing before this would leave a window in which the probe
    # raises, the router import registers sys.modules["platform.overlay_store"]
    # or attaches the attribute, and the pre-fix _store() then succeeds anyway.
    # Probe immediately before the call so check and use see the same state.
    from routers import overlay as overlay_router

    try:
        from platform import overlay_store as _pre_fix_import  # noqa: F401
    except ImportError:
        pass  # precondition holds: the pre-fix implementation fails here
    else:
        pytest.fail(
            "precondition absent: `from platform import overlay_store` SUCCEEDS "
            f"here (resolved {getattr(_pre_fix_import, '__file__', '?')}), so "
            "the pre-fix implementation would pass this test and the test "
            "cannot observe the defect it guards. Run from server/ with a "
            "sys.modules that has no 'platform' package entry.")

    store = overlay_router._store()
    # Origin, not truthiness: a module that resolved to the wrong package would
    # still be an object, and would still answer some of these names.
    assert store.__name__.endswith("overlay_store"), store.__name__
    assert Path(store.__file__).resolve() == (
        SERVER_DIR.parent / "platform" / "overlay_store.py").resolve(), store.__file__
    for name in ("document", "effective_tokens", "pending_for_session",
                 "create_proposal", "approve"):
        assert callable(getattr(store, name, None)), f"_store() lacks {name}()"


def test_store_resolution_is_stable_across_calls():
    """A second call must return the SAME module object — reloading per request
    would give two live copies of the store's module state.

    Object identity alone would also hold for a module-global cache, or for an
    import finding a pre-existing sys.modules entry, so it does not by itself
    prove the caching mechanism. The sys.modules assertion is what makes the
    first sentence true.
    """
    import sys

    from routers import overlay as overlay_router

    first = overlay_router._store()
    assert overlay_router._store() is first
    # The alias is `leaf_platform`, platform_link's — one package import in the
    # process. The router briefly registered a SECOND file-location alias
    # (`leaf_platform_pkg`), which loaded the package twice: two db modules,
    # two connection pools (sol-critic PR #439 round 6). Asserting the shared
    # alias is what keeps that from coming back.
    assert sys.modules.get("leaf_platform.overlay_store") is first
