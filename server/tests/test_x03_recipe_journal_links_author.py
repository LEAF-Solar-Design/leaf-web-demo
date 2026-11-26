"""Verify recipe behavior at a trusted, root-confined host adapter boundary."""
from dataclasses import dataclass
from hashlib import sha256
import json

import httpx
import pytest

from test_integration_element_save import recipe, rig, save
from tools_fallback import author_tool


CASES = [
    ("Recover the receipt for the inspection recipe I just saved.", "journal-link"),
    ("Check whether my recipe update saved after the lost response.", "directory-junction"),
    ("Retry the saved inspection table edit after the disconnect.", "linked-parent"),
]


@dataclass(eq=False)
class Node:
    children: dict
    target: object = None
    data: bytes = b""


class JournalAdapter:
    """Model resolved filesystem objects, including a link on any path component."""

    def __init__(self, root, owned):
        self.root = root
        self.owned = owned
        self.io = []

    def resolve(self, path):
        node = self.root
        for name in path:
            child = node.children[name]
            node = child.target or child
            if node not in self.owned:
                raise PermissionError("journal outside owned root")
        return node

    def record(self, path, intent):
        node = self.resolve(path)
        self.io.append(("write", node))
        node.data = json.dumps(intent).encode()

    def read(self, path):
        node = self.resolve(path)
        self.io.append(("read", node))
        return node.data


@pytest.mark.parametrize("ask,link", CASES)
def test_trusted_journal_refusal(save, ask, link):
    scope = save.auth.tenant_id, save.auth.session_id, save.auth.turn_id
    item = {**recipe(), "id": "inspection-east"}
    parent = {"element": item, "revision": 1}
    successor = {"element": {**item, "title": "East roof"}, "revision": 2}
    writes, reads = [], []
    def commit(request):
        writes.append(json.loads(request.content))
        raise httpx.ReadTimeout("lost acknowledgement")
    def read_revision(authority, identity, revision):
        reads.append((authority, identity, revision))
        return successor
    save.svc.transport = httpx.MockTransport(commit)
    owned_file = Node({})
    directory = Node({"receipt": owned_file})
    root = Node({"work": directory})
    foreign_file = Node({}, data=b"external sentinel")
    outside = Node({"receipt": foreign_file, "work": Node({"receipt": foreign_file})})
    if link == "journal-link":
        directory.children["alternate"] = Node({}, target=foreign_file)
        path = ("work", "alternate")
    elif link == "directory-junction":
        root.children["alternate"] = Node({}, target=outside)
        path = ("alternate", "receipt")
    else:
        root.children["alternate"] = Node({}, target=outside)
        path = ("alternate", "work", "receipt")
    journal = JournalAdapter(root, {root, directory, owned_file})
    ordinary = ("work", "receipt")
    def submit(at):
        return save.svc.edit_inspection_recipe("Retitle the inspection table as East roof.", parent,
            *scope, record_attempt=lambda _, intent: journal.record(at, intent))
    assert author_tool(ask)[0]["engine_op"] == "recipe_save_recovery"
    assert submit(path)["error"]["code"] == "unavailable"
    assert journal.io == writes == []

    assert submit(ordinary)["error"]["code"] == "outcome_unknown"
    pin = sha256(owned_file.data).hexdigest()
    def recover(at):
        try:
            data = journal.read(at)
        except PermissionError:
            data = None
        return save.svc.recover_recipe_save(ask, data, *scope, journal_sha256=pin,
                                            read_revision=read_revision)
    assert recover(path)["error"]["code"] == "outcome_unknown"
    assert reads == [] and journal.io == [("write", owned_file)]
    assert recover(ordinary) == {"result": successor}
    assert reads == [(save.auth, item["id"], 2)]
    assert journal.io == [("write", owned_file), ("read", owned_file)]
    assert writes == [successor["element"]] and foreign_file.data == b"external sentinel"
