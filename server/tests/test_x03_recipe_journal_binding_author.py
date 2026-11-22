"""Resolve a journal only with the trusted digest for the selected save attempt."""
from dataclasses import replace
from hashlib import sha256
import json
from uuid import uuid4

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover my inspection recipe save receipt for this project.", "project"),
    ("Did the selected table edit save before the reply timed out?", "table"),
    ("Retry the element update from the selected parent revision.", "parent"),
]


@pytest.mark.parametrize("ask,change", CASES)
def test_journal_belongs_to_selected_attempt(save, tmp_path, ask, change):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    first = (save.auth.project_id, "inspection-east", 1)
    second = (str(uuid4()) if change == "project" else first[0],
              "inspection-west" if change == "table" else first[1],
              2 if change == "parent" else first[2])
    receipts = {}
    digests = {}
    writes, reads = [], []

    def select_project(project):
        save.current = replace(save.current, authority=replace(save.auth, project_id=project))

    def commit(request):
        assert request.method == "PUT"
        project = request.url.path.split("/")[4]
        element = json.loads(request.content)
        parent = int(request.headers["If-Match"])
        writes.append((project, element["id"], parent))
        return httpx.Response(200, json={"revision": parent + 1, "element": element})

    def reader(authority, element_id, number):
        key = (authority.project_id, element_id, number - 1)
        reads.append(key)
        return receipts[key]

    save.svc.transport = httpx.MockTransport(commit)
    attempts = [("source", first)]
    if change != "project":
        attempts.append(("target", second))
    for label, key in attempts:
        project, element_id, parent = key
        select_project(project)
        original = ({**recipe(), "id": element_id} if change != "parent" or key == first
                    else receipts[first]["element"])
        journal = tmp_path / label
        outcome = save.svc.edit_inspection_recipe(
            "Retitle the inspection table as West roof.", {"element": original, "revision": parent}, *scope,
            record_attempt=lambda _, entry: journal.write_bytes(json.dumps(entry).encode()))
        assert "result" in outcome, outcome
        receipts[key] = outcome["result"]
        # Persist the trusted pin separately from replaceable journal contents.
        digests[key] = sha256(journal.read_bytes()).hexdigest()
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"

    def recover(data, pin):
        return save.svc.recover_recipe_save(ask, data, *scope, journal_sha256=pin, read_revision=reader)

    source = (tmp_path / "source").read_bytes()
    if change == "project":
        # Even the correct source pin cannot change the current project's scope.
        select_project(second[0])
        refused = recover(source, digests[first])
    else:
        assert digests[first] != digests[second]
        refused = recover(source, digests[second])
    assert refused["error"]["code"] == "outcome_unknown" and "result" not in refused
    expected_writes = [key for _, key in attempts]
    assert reads == [] and writes == expected_writes
    if change != "project":
        target = (tmp_path / "target").read_bytes()
        assert recover(target, digests[second]) == {"result": receipts[second]}
    select_project(first[0])
    assert recover(source, digests[first]) == {"result": receipts[first]}
    assert reads == list(reversed(expected_writes)) and writes == expected_writes
