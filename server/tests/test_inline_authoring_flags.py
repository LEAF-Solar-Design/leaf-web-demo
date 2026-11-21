import pytest

from customization_flags import enabled, inline_review_enabled, plan_mode_enabled, self_approval_allowed


@pytest.mark.parametrize("setting", ["off", "internal", "all", "garbage", None])
@pytest.mark.parametrize("tenant", ["acme", "outside"])
@pytest.mark.parametrize("staging", [True, False])
@pytest.mark.parametrize("publish", [True, False])
def test_rollout_matrix(monkeypatch, setting, tenant, staging, publish):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "alpha, acme ")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all" if staging else "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all" if publish else "off")
    for name in ("LEAF_STUDIO_PLAN_MODE", "LEAF_STUDIO_INLINE_REVIEW"):
        if setting is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, setting)
    internal = setting == "internal" and tenant == "acme"
    eligible = setting == "all" or internal
    assert plan_mode_enabled(tenant) is (staging and eligible)
    assert inline_review_enabled(tenant) is (staging and publish and eligible)
    assert self_approval_allowed(tenant) is (staging and publish and internal)
    assert enabled(5, tenant) is staging
    assert enabled(6, tenant) is (staging and publish)


def test_flags_are_independent_and_self_approval_requires_exact_mode(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "acme")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "off")
    monkeypatch.setenv("LEAF_STUDIO_INLINE_REVIEW", " Internal ")
    assert not plan_mode_enabled("acme")
    assert inline_review_enabled("acme")
    assert not self_approval_allowed("acme")
    assert not inline_review_enabled("outside")
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "all")
    monkeypatch.setenv("LEAF_STUDIO_INLINE_REVIEW", "off")
    assert plan_mode_enabled("acme")
    assert not plan_mode_enabled("outside")
    assert not inline_review_enabled("acme")
