# Native Solar edited saves

This is part of S01 production hardening for the shared project drawing lifecycle. The worker export boundary and server admission serve different roles: the worker emits a consistent native document; admission binds that document to its verified parent before publication.

For a native parent, generic editor save uses the unchanged strict native DXF decoder. Scope, units, generation inputs, provenance, frame membership and attributes, and non-frame state must match the verified parent. Only rigid frame pose changes are admitted. The resulting intake stores the physical document decoded from the submitted bytes. Save receipt hashes still identify those exact bytes.

Server-owned restore and Solar publication retain their existing internal intake path. Client requests cannot select that override. No route, grant or persistent schema is added.

## Replay

From the repository root, use the canonical disposable PostgreSQL producer and then run:

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_project_drawing_editor.py server/tests/test_project_solar_materialization.py -q
```

The existing private database guard must verify the real producer before migration. Never point these fixtures at a developer, shared staging or production database, and never synthesize a managed-producer marker. Without a verified producer, database cases skip and do not prove publication.

The local candidate result is 104 executed cases with no skips. It includes a canonical initial native layout, a rigid edited save through the real HTTP route and PostgreSQL publication store, exact source reopen, matching physical inspection, retry after checkout release, and scope/provenance/resize refusal with no new version. Sixteen ordinary admission controls cover attribute, membership, units and stale-geometry refusals plus the save handoff.

Fixture tenants and checkout signing are synthetic. The immutable blob backend is in memory and the active-tenant dependency is supplied by the fixture. This proves local HTTP, checkout, PostgreSQL publication and reopen behavior; it does not prove an external identity grant, cloud object storage, browser interaction or LIVE deployment. Worker-specific runtime and replay boundaries are documented in `solar-native-worker-export.md`.

Earlier corrupted-state continuity tests seed their invalid server-owned state through the explicit internal restore intake seam. The ordinary editor route now refuses that invalid input before publication. Storage replay tests use actual native producer bytes rather than an unrelated DXF carrying native metadata. Existing assertions remain intact.
