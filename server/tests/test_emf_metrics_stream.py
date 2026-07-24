from __future__ import annotations

import json

import emf_metrics


def test_emf_uses_stderr_and_preserves_stdout_protocol(capsys, monkeypatch):
    monkeypatch.setattr(emf_metrics, "_DISABLED", False)
    emf_metrics.emit_job_terminal("complete", "local")

    captured = capsys.readouterr()
    assert captured.out == ""
    document = json.loads(captured.err)
    assert document["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Leaf/Platform/APS"
    assert document["JobTerminal"] == 1
    assert document["status"] == "complete"
