"""URI-looking dependency references stay inert through intake and saved replay."""
import json
import socket
import urllib.request
from dxf_intake import parse_dxf_bytes
import tool_loader
from test_x04_minsert_arrays import TOOL, selected
from test_x04_original_bytes_replay import RAW


def test_uri_names_never_open_network_or_url_resources(monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Dependency references must stay inert")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    names = ["http://synthetic.invalid/a.shx", "https://synthetic.invalid/b.shx", "file:///synthetic/external.shx"]
    styles = "".join(f"0\nSTYLE\n2\nS{i}\n3\n{name}\n" for i,name in enumerate(names)).encode()
    # Preserve the existing synthetic drawing's real BLOCK definition/INSERT topology.
    raw = RAW.replace(b"0\nSTYLE\n2\nNotes\n3\nnotes.shx\n", styles)
    intake = parse_dxf_bytes(raw)
    saved = json.loads(json.dumps(intake, allow_nan=False))
    assert saved["dependency_report"] == intake["dependency_report"]
    assert {row["name"] for row in saved["dependency_report"]["dependencies"]} == set(names)
    assert all(row["disposition"] == "font remapped" for row in saved["dependency_report"]["dependencies"])
    outcome = selected(saved)
    assert outcome["ok"] and outcome["result"]["count"] == 7
    assert outcome["overlay"]["highlight_handles"] == ["A", "B"] and attempts == []
