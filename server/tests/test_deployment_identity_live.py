"""The live-derived identity must never report a stale receipt as success."""
import json

import pytest

import deployment_identity_live as live
from deployment_identity_live import (
    LiveIdentityUnavailable,
    live_deployment_identity,
)


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
REVISION = "1866142585455f9ed75c7a4292fcaad392146d04"
OTHER_REVISION = "0c59f7817fd7" + "0" * 28


def digest(seed):
    return "sha256:" + (seed * 64)[:64]


LIVE = {name: digest(chr(ord("a") + index)) for index, name in enumerate(SERVICES)}


@pytest.fixture(autouse=True)
def _clear_cache():
    live.reset_cache()
    yield
    live.reset_cache()


def reader(digests):
    def _read(_env):
        return dict(digests)

    return _read


def receipt(services, environment="staging", revision=REVISION):
    return json.dumps(
        {
            "schema": "leaf.deployment-identity.v1",
            "environment": environment,
            "source_revision": revision,
            "services": services,
        }
    )


def coherent_receipt(digests=None, revision=REVISION):
    source = digests or LIVE
    return receipt(
        {
            name: {"image_digest": source[name], "source_revision": revision}
            for name in SERVICES
        },
        revision=revision,
    )


def test_digests_always_come_from_live_state_not_the_receipt():
    env = {
        "LEAF_DEPLOYMENT_IDENTITY": coherent_receipt(
            {name: digest("f") for name in SERVICES}
        )
    }

    result = live_deployment_identity(env, reader=reader(LIVE))

    for name in SERVICES:
        assert result["services"][name]["image_digest"] == LIVE[name]


def test_fully_coherent_receipt_verifies():
    env = {"LEAF_DEPLOYMENT_IDENTITY": coherent_receipt()}

    result = live_deployment_identity(env, reader=reader(LIVE))

    assert result["status"] == "verified"
    assert result["source_revision"] == REVISION
    assert all(result["services"][name]["attested"] for name in SERVICES)


def test_the_measured_staging_staleness_reports_mismatch_not_success():
    """Regression for the 2026-08-24 live measurement.

    app-alt:139 certified web sha256:1d0460ed... while leaf-platform-web:252
    was running sha256:a12a9d7c.... Broker, canonical-worker and harness all
    still matched, so three of five entries were true. That receipt must not
    read as a pass.
    """
    stale = dict(LIVE)
    stale["web"] = digest("9")
    env = {"LEAF_DEPLOYMENT_IDENTITY": coherent_receipt(stale)}

    result = live_deployment_identity(env, reader=reader(LIVE))

    assert result["status"] == "mismatch"
    assert "source_revision" not in result
    assert result["services"]["web"]["image_digest"] == LIVE["web"]
    assert result["services"]["web"]["receipt_claims_digest"] == stale["web"]
    assert result["services"]["web"]["attested"] is False
    # The three genuinely-matching services stay attested; the response is
    # precise about WHICH service is wrong rather than collapsing to a blob.
    assert result["services"]["broker"]["attested"] is True


def test_a_stale_entry_never_contributes_its_source_revision():
    stale = dict(LIVE)
    stale["app"] = digest("9")
    services = {
        name: {
            "image_digest": stale[name],
            "source_revision": OTHER_REVISION if name == "app" else REVISION,
        }
        for name in SERVICES
    }
    env = {"LEAF_DEPLOYMENT_IDENTITY": receipt(services)}

    result = live_deployment_identity(env, reader=reader(LIVE))

    assert result["status"] == "mismatch"
    assert "source_revision" not in result["services"]["app"]


def test_absent_receipt_still_returns_truthful_live_digests():
    result = live_deployment_identity({}, reader=reader(LIVE))

    assert result["status"] == "unattested"
    assert "source_revision" not in result
    for name in SERVICES:
        assert result["services"][name]["image_digest"] == LIVE[name]
        assert result["services"][name]["attested"] is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"schema": "wrong"}),
        json.dumps({"schema": "leaf.deployment-identity.v1"}),
        json.dumps({"schema": "leaf.deployment-identity.v1", "services": []}),
    ],
)
def test_an_unusable_receipt_degrades_to_unattested_never_to_a_wrong_answer(raw):
    result = live_deployment_identity(
        {"LEAF_DEPLOYMENT_IDENTITY": raw}, reader=reader(LIVE)
    )

    assert result["status"] == "unattested"
    for name in SERVICES:
        assert result["services"][name]["image_digest"] == LIVE[name]


def test_a_receipt_for_another_environment_attests_nothing():
    env = {
        "LEAF_DEPLOYMENT_IDENTITY": receipt(
            {
                name: {"image_digest": LIVE[name], "source_revision": REVISION}
                for name in SERVICES
            },
            environment="production",
        )
    }

    result = live_deployment_identity(env, reader=reader(LIVE))

    assert result["status"] == "unattested"


def test_mixed_revisions_across_services_do_not_verify():
    services = {
        name: {
            "image_digest": LIVE[name],
            "source_revision": OTHER_REVISION if name == "web" else REVISION,
        }
        for name in SERVICES
    }

    result = live_deployment_identity(
        {"LEAF_DEPLOYMENT_IDENTITY": receipt(services)}, reader=reader(LIVE)
    )

    assert result["status"] == "unattested"
    assert "source_revision" not in result


def test_partial_receipt_coverage_does_not_verify():
    services = {
        name: {"image_digest": LIVE[name], "source_revision": REVISION}
        for name in ("app", "broker")
    }

    result = live_deployment_identity(
        {"LEAF_DEPLOYMENT_IDENTITY": receipt(services)}, reader=reader(LIVE)
    )

    assert result["status"] == "unattested"


def test_unreadable_live_state_fails_closed_and_never_serves_the_receipt():
    def explode(_env):
        raise LiveIdentityUnavailable("ECS is unreachable")

    with pytest.raises(LiveIdentityUnavailable):
        live_deployment_identity(
            {"LEAF_DEPLOYMENT_IDENTITY": coherent_receipt()}, reader=explode
        )


def test_incomplete_live_digest_set_fails_closed():
    with pytest.raises(LiveIdentityUnavailable):
        live_deployment_identity({}, reader=reader({"app": LIVE["app"]}))


def test_reads_are_cached_so_a_burst_makes_one_ecs_call():
    calls = []

    def counting(_env):
        calls.append(1)
        return dict(LIVE)

    for _ in range(5):
        live_deployment_identity({}, reader=counting)

    assert len(calls) == 1


def test_production_requires_an_explicit_environment_binding():
    with pytest.raises(LiveIdentityUnavailable):
        live_deployment_identity(
            {"LEAF_RUNTIME_ENV": "production"}, reader=reader(LIVE)
        )

    result = live_deployment_identity(
        {
            "LEAF_RUNTIME_ENV": "production",
            "LEAF_DEPLOYMENT_ENVIRONMENT": "production",
        },
        reader=reader(LIVE),
    )
    assert result["environment"] == "production"


class _FakeClient:
    def __init__(self, services, task_definitions):
        self._services = services
        self._task_definitions = task_definitions

    def describe_services(self, cluster, services):  # noqa: ARG002
        return {
            "services": [
                self._services[name] for name in services if name in self._services
            ]
        }

    def describe_task_definition(self, taskDefinition):  # noqa: N803
        return {"taskDefinition": self._task_definitions[taskDefinition]}


def _service(name, td, desired=1, running=1):
    return {
        "serviceName": name,
        "taskDefinition": td,
        "desiredCount": desired,
        "runningCount": running,
    }


def _td(container, image):
    return {"containerDefinitions": [{"name": container, "image": image}]}


def _staging_fixture(app_active="leaf-platform-app", web_active="leaf-platform-web"):
    """Mirror the real staging shape: color pairs, one active per service."""
    services = {}
    for family, active in (
        ("leaf-platform-app", app_active),
        ("leaf-platform-app-alt", app_active),
        ("leaf-platform-web", web_active),
        ("leaf-platform-web-alt", web_active),
    ):
        services[family] = _service(
            family, f"{family}:1", desired=1 if family == active else 0,
            running=1 if family == active else 0,
        )
    for family in (
        "leaf-platform-broker",
        "leaf-platform-canonical-worker",
        "leaf-platform-harness",
    ):
        services[family] = _service(family, f"{family}:1")
    task_definitions = {
        "leaf-platform-app:1": _td(
            "leaf-platform-app", f"repo/leaf-platform-app@{LIVE['app']}"
        ),
        "leaf-platform-app-alt:1": _td(
            "leaf-platform-app", f"repo/leaf-platform-app@{digest('9')}"
        ),
        "leaf-platform-web:1": _td(
            "leaf-platform-web", f"repo/leaf-platform-web@{LIVE['web']}"
        ),
        "leaf-platform-web-alt:1": _td(
            "leaf-platform-web", f"repo/leaf-platform-web@{digest('8')}"
        ),
        "leaf-platform-broker:1": _td(
            "leaf-platform-broker", f"repo/leaf-platform-broker@{LIVE['broker']}"
        ),
        "leaf-platform-canonical-worker:1": _td(
            "leaf-platform-canonical-worker",
            f"repo/leaf-platform-canonical-worker@{LIVE['canonical-worker']}",
        ),
        "leaf-platform-harness:1": _td(
            "leaf-platform-harness", f"repo/leaf-platform-harness@{LIVE['harness']}"
        ),
    }
    return _FakeClient(services, task_definitions)


def test_the_routed_color_is_the_one_that_is_read(monkeypatch):
    monkeypatch.setattr(live, "_get_client", lambda _env: _staging_fixture())

    digests = live.live_digests({})

    assert digests["app"] == LIVE["app"]
    assert digests["web"] == LIVE["web"]


def test_the_idle_color_is_read_when_it_is_the_routed_one(monkeypatch):
    monkeypatch.setattr(
        live, "_get_client", lambda _env: _staging_fixture(app_active="leaf-platform-app-alt")
    )

    digests = live.live_digests({})

    assert digests["app"] == digest("9")


def test_two_active_colors_fail_closed_rather_than_pick_one(monkeypatch):
    client = _staging_fixture()
    client._services["leaf-platform-app-alt"] = _service(
        "leaf-platform-app-alt", "leaf-platform-app-alt:1"
    )
    monkeypatch.setattr(live, "_get_client", lambda _env: client)

    with pytest.raises(LiveIdentityUnavailable, match="ambiguous"):
        live.live_digests({})


def test_no_active_color_fails_closed(monkeypatch):
    client = _staging_fixture()
    for family in ("leaf-platform-app", "leaf-platform-app-alt"):
        client._services[family] = _service(family, f"{family}:1", desired=0, running=0)
    monkeypatch.setattr(live, "_get_client", lambda _env: client)

    with pytest.raises(LiveIdentityUnavailable, match="no active app"):
        live.live_digests({})


def test_a_tag_reference_is_refused_because_it_is_not_immutable(monkeypatch):
    client = _staging_fixture()
    client._task_definitions["leaf-platform-app:1"] = _td(
        "leaf-platform-app", "repo/leaf-platform-app:sha-236d803d"
    )
    monkeypatch.setattr(live, "_get_client", lambda _env: client)

    with pytest.raises(LiveIdentityUnavailable, match="tag reference"):
        live.live_digests({})


def test_sidecar_containers_do_not_shadow_the_service_image(monkeypatch):
    """The live app task definition carries two init containers.

    Reading the first container rather than the named one would compare the
    ios-provider or fence sidecar digest against the app receipt entry.
    """
    client = _staging_fixture()
    client._task_definitions["leaf-platform-app:1"] = {
        "containerDefinitions": [
            {"name": "init-drawing-mutations-fence", "image": f"repo/other@{digest('7')}"},
            {"name": "init-ios-provider-files", "image": f"repo/ios@{digest('6')}"},
            {"name": "leaf-platform-app", "image": f"repo/leaf-platform-app@{LIVE['app']}"},
        ]
    }
    monkeypatch.setattr(live, "_get_client", lambda _env: client)

    assert live.live_digests({})["app"] == LIVE["app"]
