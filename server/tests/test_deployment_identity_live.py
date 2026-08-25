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
    """Mirrors the ECS calls the reader actually makes.

    DescribeServices, ListTasks, DescribeTasks. Deliberately exposes no
    describe_task_definition, so a regression back to that action fails loudly
    here rather than only at the IAM boundary in production.
    """

    def __init__(self, services, tasks_by_service, task_detail):
        self._services = services
        self._tasks_by_service = tasks_by_service
        self._task_detail = task_detail

    def describe_services(self, cluster, services):  # noqa: ARG002
        return {
            "services": [
                self._services[name] for name in services if name in self._services
            ]
        }

    def list_tasks(self, cluster, serviceName, desiredStatus):  # noqa: N803, ARG002
        return {"taskArns": list(self._tasks_by_service.get(serviceName, []))}

    def describe_tasks(self, cluster, tasks):  # noqa: ARG002
        return {"tasks": [self._task_detail[arn] for arn in tasks]}


def _service(name, desired=1, running=1):
    return {"serviceName": name, "desiredCount": desired, "runningCount": running}


def _task(container, image_digest):
    return {"containers": [{"name": container, "imageDigest": image_digest}]}


def _staging_fixture(app_active="leaf-platform-app", web_active="leaf-platform-web"):
    """Mirror the real staging shape: color pairs, one active per service."""
    services = {}
    for family in (
        "leaf-platform-app",
        "leaf-platform-app-alt",
        "leaf-platform-web",
        "leaf-platform-web-alt",
    ):
        active = app_active if "app" in family else web_active
        services[family] = _service(
            family,
            desired=1 if family == active else 0,
            running=1 if family == active else 0,
        )
    for family in (
        "leaf-platform-broker",
        "leaf-platform-canonical-worker",
        "leaf-platform-harness",
    ):
        services[family] = _service(family)

    tasks_by_service = {family: ["task/" + family + "/1"] for family in services}
    task_detail = {
        "task/leaf-platform-app/1": _task("leaf-platform-app", LIVE["app"]),
        "task/leaf-platform-app-alt/1": _task("leaf-platform-app", digest("9")),
        "task/leaf-platform-web/1": _task("leaf-platform-web", LIVE["web"]),
        "task/leaf-platform-web-alt/1": _task("leaf-platform-web", digest("8")),
        "task/leaf-platform-broker/1": _task("leaf-platform-broker", LIVE["broker"]),
        "task/leaf-platform-canonical-worker/1": _task(
            "leaf-platform-canonical-worker", LIVE["canonical-worker"]
        ),
        "task/leaf-platform-harness/1": _task("leaf-platform-harness", LIVE["harness"]),
    }
    return _FakeClient(services, tasks_by_service, task_detail)


_SIDECAR_FAMILIES = (
    "leaf-platform-app",
    "leaf-platform-app-alt",
    "leaf-platform-web",
    "leaf-platform-web-alt",
    "leaf-platform-broker",
    "leaf-platform-harness",
    "leaf-platform-canonical-worker",
)


def _sidecar_env(tmp_path, client, observed_at=None, state="ok", reason=None):
    """Materialize a fake client's world into the sidecar document.

    The reader consumes the collector's file now; the fixtures still describe
    the fleet through the same three ECS response shapes, so the behavioral
    tests keep exercising identical routing/digest logic. The bridge calls
    only the three reviewed read actions, so the DescribeTaskDefinition
    regression guard keeps its teeth.
    """
    import datetime as _dt
    import json as _json

    document = {
        "schema": "leaf.live-identity-collector.v1",
        "observed_at": observed_at
        or _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "state": state,
        "describe_services": client.describe_services("c", list(_SIDECAR_FAMILIES)),
        "tasks": {},
    }
    if reason is not None:
        document["reason"] = reason
    for family in _SIDECAR_FAMILIES:
        arns = client.list_tasks("c", serviceName=family, desiredStatus="RUNNING")[
            "taskArns"
        ]
        if arns:
            document["tasks"][family] = client.describe_tasks("c", tasks=arns)
    path = tmp_path / "current.json"
    path.write_text(_json.dumps(document), encoding="utf-8")
    return {"LEAF_IDENTITY_SIDECAR_FILE": str(path)}


def test_the_routed_color_is_the_one_that_is_read(tmp_path):
    env = _sidecar_env(tmp_path, _staging_fixture())

    digests = live.live_digests(env)

    assert digests["app"] == LIVE["app"]
    assert digests["web"] == LIVE["web"]


def test_the_idle_color_is_read_when_it_is_the_routed_one(tmp_path):
    env = _sidecar_env(
        tmp_path, _staging_fixture(app_active="leaf-platform-app-alt")
    )

    assert live.live_digests(env)["app"] == digest("9")


def test_two_active_colors_fail_closed_rather_than_pick_one(tmp_path):
    client = _staging_fixture()
    client._services["leaf-platform-app-alt"] = _service("leaf-platform-app-alt")

    with pytest.raises(LiveIdentityUnavailable, match="ambiguous"):
        live.live_digests(_sidecar_env(tmp_path, client))


def test_no_active_color_fails_closed(tmp_path):
    client = _staging_fixture()
    for family in ("leaf-platform-app", "leaf-platform-app-alt"):
        client._services[family] = _service(family, desired=0, running=0)

    with pytest.raises(LiveIdentityUnavailable, match="no active app"):
        live.live_digests(_sidecar_env(tmp_path, client))


def test_a_mid_roll_service_running_two_digests_fails_closed(tmp_path):
    """The case an arbitrary pick would answer wrongly.

    While a service rolls, two tasks run different images. There is no single
    true running identity then, so refuse rather than report whichever task
    happened to sort first.
    """
    client = _staging_fixture()
    client._tasks_by_service["leaf-platform-web"] = [
        "task/leaf-platform-web/1",
        "task/leaf-platform-web/2",
    ]
    client._task_detail["task/leaf-platform-web/2"] = _task(
        "leaf-platform-web", digest("5")
    )

    with pytest.raises(LiveIdentityUnavailable, match="mid-roll"):
        live.live_digests(_sidecar_env(tmp_path, client))


def test_several_tasks_on_one_digest_are_fine(tmp_path):
    """Horizontal scale is not a mid-roll: same digest, one true answer."""
    client = _staging_fixture()
    client._tasks_by_service["leaf-platform-web"] = [
        "task/leaf-platform-web/1",
        "task/leaf-platform-web/2",
    ]
    client._task_detail["task/leaf-platform-web/2"] = _task(
        "leaf-platform-web", LIVE["web"]
    )

    assert live.live_digests(_sidecar_env(tmp_path, client))["web"] == LIVE["web"]


def test_a_service_with_no_running_tasks_fails_closed(tmp_path):
    client = _staging_fixture()
    client._tasks_by_service["leaf-platform-web"] = []

    with pytest.raises(LiveIdentityUnavailable, match="no running tasks"):
        live.live_digests(_sidecar_env(tmp_path, client))


def test_a_container_without_a_digest_is_refused(tmp_path):
    """imageDigest is absent for a non-ECR registry; there is no honest answer."""
    client = _staging_fixture()
    client._task_detail["task/leaf-platform-web/1"] = {
        "containers": [{"name": "leaf-platform-web"}]
    }

    with pytest.raises(LiveIdentityUnavailable, match="no usable image digest"):
        live.live_digests(_sidecar_env(tmp_path, client))


def test_sidecar_containers_do_not_shadow_the_service_image(tmp_path):
    """The live app task carries two init containers alongside the app."""
    client = _staging_fixture()
    client._task_detail["task/leaf-platform-app/1"] = {
        "containers": [
            {"name": "init-drawing-mutations-fence", "imageDigest": digest("7")},
            {"name": "init-ios-provider-files", "imageDigest": digest("6")},
            {"name": "leaf-platform-app", "imageDigest": LIVE["app"]},
        ]
    }

    assert live.live_digests(_sidecar_env(tmp_path, client))["app"] == LIVE["app"]


def test_the_reader_never_calls_describe_task_definition(tmp_path):
    """Regression guard on the IAM shape.

    DescribeTaskDefinition cannot be resource-scoped by AWS, so granting it
    would mean account-wide read of every task definition, including other
    services' plaintext environment variables. The reader must never need it.
    """
    client = _staging_fixture()

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "reader called describe_task_definition; that action cannot be "
            "resource-scoped and must not be required"
        )

    client.describe_task_definition = forbidden

    assert set(live.live_digests(_sidecar_env(tmp_path, client))) == set(SERVICES)


def test_a_stale_sidecar_document_fails_closed(tmp_path):
    import datetime as _dt

    old = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)
    ).isoformat()
    env = _sidecar_env(tmp_path, _staging_fixture(), observed_at=old)

    with pytest.raises(LiveIdentityUnavailable, match="stale"):
        live.live_digests(env)


def test_a_future_dated_sidecar_document_fails_closed(tmp_path):
    import datetime as _dt

    ahead = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=60)
    ).isoformat()
    env = _sidecar_env(tmp_path, _staging_fixture(), observed_at=ahead)

    with pytest.raises(LiveIdentityUnavailable, match="future-dated"):
        live.live_digests(env)


def test_an_unavailable_sidecar_state_surfaces_its_reason(tmp_path):
    env = _sidecar_env(
        tmp_path, _staging_fixture(), state="unavailable", reason="ThrottlingException: rate"
    )

    with pytest.raises(LiveIdentityUnavailable, match="ThrottlingException"):
        live.live_digests(env)


def test_an_absent_sidecar_file_fails_closed(tmp_path):
    with pytest.raises(LiveIdentityUnavailable, match="absent"):
        live.live_digests({"LEAF_IDENTITY_SIDECAR_FILE": str(tmp_path / "missing.json")})


def test_the_response_names_the_sidecar_and_its_observation(tmp_path):
    env = _sidecar_env(tmp_path, _staging_fixture())

    result = live_deployment_identity(env)

    assert result["derived_from"] == "live-ecs-sidecar"
    assert isinstance(result["observed_at"], str)
    assert isinstance(result["age_seconds"], float)
    assert result["age_seconds"] < live._SIDECAR_MAX_AGE_SECONDS
