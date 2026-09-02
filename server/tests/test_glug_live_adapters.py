import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import glug_adoption
import glug_live_adapters
from glug_executor import GlugExecutor, GlugExecutorError
from glug_live_adapters import GitHubReviewProvider, HarnessAuthorAdapter


def test_configured_executor_mounts_every_live_adapter(tmp_path, monkeypatch):
    source = tmp_path / "source"
    workspace = tmp_path / "workspaces"
    artifact = tmp_path / "artifact"
    for path in (source, workspace, artifact):
        path.mkdir()
    author, approvals, provider = object(), object(), object()
    monkeypatch.setattr(
        glug_live_adapters, "configured_live_components",
        lambda env, **kwargs: (author, approvals, provider),
    )
    monkeypatch.setattr(glug_adoption, "load_adoption", lambda: object())
    monkeypatch.setattr(glug_adoption, "verify_artifact_tree", lambda adoption, root: None)
    monkeypatch.setenv("GLUG_MUSHY_CANONICAL_GIT_SOURCE", str(source))
    monkeypatch.setenv("GLUG_MUSHY_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("LEAF_GLUG_MUSHY_ARTIFACT_ROOT", str(artifact))
    monkeypatch.setenv("GLUG_MUSHY_CLAIM_SIGNING_SECRET", "x" * 32)
    executor = GlugExecutor.configured()
    assert executor.author is author
    assert executor.approvals is approvals
    assert executor.provider is provider


def test_configured_executor_fails_closed_when_mount_config_is_missing(monkeypatch):
    for key in (
        "GLUG_MUSHY_CANONICAL_GIT_SOURCE", "GLUG_MUSHY_WORKSPACE_ROOT",
        "LEAF_GLUG_MUSHY_ARTIFACT_ROOT",
        "GLUG_MUSHY_JOB_DATABASE", "GLUG_GITHUB_REVIEW_TOKEN", "GLUG_REPOSITORY_SLUG",
        "GLUG_MUSHY_AUTHOR_URL", "LEAF_HARNESS_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(GlugExecutorError, match="not configured"):
        GlugExecutor.configured()


def test_author_prompt_crosses_only_the_authenticated_harness_body(tmp_path):
    captured = {}
    class Response:
        status = 200
        def read(self, _limit):
            return b"{}"
        def close(self):
            captured["closed"] = True
    def open_request(request, **kwargs):
        captured.update(request=request, kwargs=kwargs)
        return Response()
    prompt = "Change the private weekend welcome copy"
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    entrypoint_payload = b"export const pinned = true;"
    entrypoint_path = artifact / "index.js"
    entrypoint_path.write_bytes(entrypoint_payload)
    entrypoint = glug_adoption.ArtifactFile(
        "index.js", len(entrypoint_payload),
        hashlib.sha256(entrypoint_payload).hexdigest(),
    )
    claim_id = "claim-123"
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / hashlib.sha256(claim_id.encode()).hexdigest() / "repository"
    repository.mkdir(parents=True)
    harness_secret = "harness-secret-not-for-the-author-payload"
    adapter = HarnessAuthorAdapter(
        artifact_root=artifact,
        entrypoint=entrypoint,
        endpoint="http://harness:8150/internal/glug/mushy/author",
        harness_secret=harness_secret,
        workspace_root=workspace_root,
        opener=open_request,
    )
    result = adapter.run(
        {"instruction": prompt, "power": "stage_change", "claim_id": claim_id},
        repository=repository,
        artifact_root=artifact, author_timeout_seconds=240,
        wrapper_timeout_seconds=280,
        env={"PATH": "safe", "GLUG_MUSHY_SOURCE_COMMIT": "a" * 40,
             "GLUG_GITHUB_REVIEW_TOKEN": "provider-secret", "STRIPE_SECRET_KEY": "money-secret"},
    )
    assert result == {}
    request = captured["request"]
    assert request.full_url == "http://harness:8150/internal/glug/mushy/author"
    assert prompt not in request.full_url
    assert json.loads(request.data)["instruction"] == prompt
    assert b"provider-secret" not in request.data
    assert b"money-secret" not in request.data
    assert request.get_header("X-harness-secret") == harness_secret
    assert request.get_header("X-glug-mushy-source-commit") == "a" * 40
    assert captured["kwargs"]["timeout"] == 280
    assert captured["closed"] is True


def test_author_refuses_pinned_entrypoint_digest_drift(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    payload = b"export const pinned = true;"
    entrypoint_path = artifact / "index.js"
    entrypoint_path.write_bytes(payload)
    entrypoint = glug_adoption.ArtifactFile(
        "index.js", len(payload), hashlib.sha256(payload).hexdigest())
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    adapter = HarnessAuthorAdapter(
        artifact_root=artifact,
        entrypoint=entrypoint,
        endpoint="http://harness:8150/internal/glug/mushy/author",
        harness_secret="test-secret",
        workspace_root=workspace_root,
        opener=lambda *args, **kwargs: pytest.fail("must not call harness"),
    )
    entrypoint_path.write_bytes(b"export const stale = true;")

    with pytest.raises(GlugExecutorError, match="digest drifted"):
        adapter.run(
            {"instruction": "stage safe copy", "power": "stage_change", "claim_id": "claim-123"},
            repository=tmp_path, artifact_root=artifact,
            author_timeout_seconds=240, wrapper_timeout_seconds=280,
            env={"PATH": "safe"},
        )


def test_author_closes_http_error_response(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    payload = b"export const pinned = true;"
    (artifact / "index.js").write_bytes(payload)
    entrypoint = glug_adoption.ArtifactFile(
        "index.js", len(payload), hashlib.sha256(payload).hexdigest())
    claim_id = "claim-123"
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / hashlib.sha256(claim_id.encode()).hexdigest() / "repository"
    repository.mkdir(parents=True)
    response_body = io.BytesIO(b'{"error":{"code":"request_invalid"}}')
    http_error = urllib.error.HTTPError(
        "http://harness:8150/internal/glug/mushy/author",
        422,
        "invalid",
        {},
        response_body,
    )

    def refuse_request(*_args, **_kwargs):
        raise http_error

    adapter = HarnessAuthorAdapter(
        artifact_root=artifact,
        entrypoint=entrypoint,
        endpoint="http://harness:8150/internal/glug/mushy/author",
        harness_secret="test-secret",
        workspace_root=workspace_root,
        opener=refuse_request,
    )
    with pytest.raises(GlugExecutorError, match="refused"):
        adapter.run(
            {"instruction": "stage safe copy", "power": "stage_change", "claim_id": claim_id},
            repository=repository,
            artifact_root=artifact,
            author_timeout_seconds=240,
            wrapper_timeout_seconds=280,
            env={"GLUG_MUSHY_SOURCE_COMMIT": "a" * 40},
        )
    assert response_body.closed


def test_provider_token_is_environment_only_and_provider_has_no_merge_power(tmp_path, monkeypatch):
    captured = {}
    def fake_run(argv, **kwargs):
        captured.update(argv=list(argv), env=dict(kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    token = "github-provider-token-value"
    provider = GitHubReviewProvider(token=token, repository_slug="biting-fogies/glug")
    result = provider.create_review_branch(
        repository_slug="biting-fogies/glug", repository=tmp_path,
        commit="a" * 40, branch_name="glug/mushy/aaaaaaaaaaaa",
    )
    assert result["commit"] == "a" * 40
    assert token not in " ".join(captured["argv"])
    assert token not in json.dumps(result)
    assert token not in captured["env"].get("PATH", "")
    assert any("AUTHORIZATION" in value for value in captured["env"].values())
    assert not hasattr(provider, "merge") and not hasattr(provider, "deploy")
