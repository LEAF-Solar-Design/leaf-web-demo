# X07 offline reproduction

Parent: Studio X-bench X07 drawing role discovery and reusable customer profiles.
These synthetic checks prove isolated source behavior. They do not establish
staging readiness or replace the frozen seven-step grader.

## Setup and commands

Run from the repository root with Python 3 and SQLite available. The Python
checks create disposable databases through an injected path. They use no real
drawings, environment credentials, provider grants, browser, or Mac host.

```powershell
python -m unittest discover -s server/tests -p 'test_x07_*.py'
python tools/xbench/x07_role_discovery_perf.py --smoke
python tools/xbench/x07_synthetic_journey.py
```

The browser-free UI check requires the web package dependencies, including
React, Testing Library, jsdom and esbuild. Point `--deps` at an existing install:

```powershell
node tools/xbench/tests/x07_role_confirmation_dom.cjs --deps web/node_modules
```

The same component cases are authored in
`web/src/solar/X07RoleConfirmation.test.jsx` for the normal Vitest rail.
The direct DOM harness avoids dependency on that rail's setup-file loader.
It now also checks injected copy choices and customer profile editing, including
stale-version recovery, customer switches and an older pinned project version.

The synthetic journey composes discovery, inclusive selection, explicit
customer compatibility and SQLite persistence for two customers and five
drawings. It emits seven rows of Boolean assertions. `proof_mode` is always
`synthetic_offline`; these rows are not a staging grader or readiness marker.
`--database` requires a new file and refuses an existing database.

## Seven-step mapping

| Frozen step | Executable synthetic evidence | Integration still missing |
|---|---|---|
| 1 Automatic proposal and one confirmation | Discovery failure checks keep module naming deterministic. The DOM harness chooses Panel B and permits one confirm callback. | Opening a real project must render the proposal and persist its confirmed choice. |
| 2 Competing sets and picked-copy count | Selector tests create three copies of three INSERTs. Window `[10,0,12,2]` picks indices 3, 4, 5, count 3. Other blocks/layers do not count. | A product control must supply the confirmed block, layer and design window. |
| 3 Editable customer profile with a version | Store tests save an explicit customer-a role map as version 1. Confirmation callback failures preserve the chosen set for retry. | Authenticated customer binding, save route and editable profile UI are not connected to this local store. |
| 4 Same-customer reuse | Two fresh project keys for customer-a get the same profile id, version 1 and identical roles after reopening the SQLite file. | Project-open integration must apply compatible roles automatically and prove real counts without edits. |
| 5 Other customer's reuse and copy pick | Customer-b has its own profile id. The same pure selector accepts a separate finite copy window. | Anonymous block compatibility and at-most-one-copy-pick product behavior are not implemented by the store. |
| 6 Cross-customer isolation | Applying or reading customer-a's project with customer-b raises ProfileScopeError. A customer with no profile gets no application record. | Real drawing compatibility must trigger a fresh proposal and the live applied-profile record must exclude the other customer's id. |
| 7 Version retention | Saving version 2 with expected_version=1 preserves version 1 and the old project's pin. A new project pins version 2. SQL UPDATE/DELETE of versions or pins is refused. | Editing and project-open routes must use these operations without replacing drawing material or existing project identity. |

## Expected synthetic records

`save_version('customer-a', {'module': {'block': 'Panel A', 'layer': 'MODULES A'}})`
returns customer-a, an opaque stable profile id, version 1 and that role map.
`apply_to_project('project-one', 'customer-a')` adds project-one to the same
record. Customer-b's profile id is different. Customer keys are explicit and
case-sensitive; they are never derived from a block name.

After a customer-a edit, `latest('customer-a')` has version 2 and the same profile
id. `application_record('project-one', 'customer-a')` still has version 1 and the
old map. Reading that project as customer-b refuses rather than returning data.

Selection uses inclusive XY insertion-origin bounds with no added tolerance.
Zero-area windows are allowed. Rotation, scale, Z and block extents do not alter
membership. Each input INSERT record counts once. Block/layer symbols match
case-insensitively. Invalid matching origins and non-finite or reversed windows
refuse the operation. None of these pure operations changes the input drawing.

The performance smoke checks 30 and 300 seeded INSERTs. Its elapsed time and
allocation peak cover discovery only. No browser budget or production latency
claim follows from those measurements.

Compatibility requires the caller's customer key before looking at role names.
Missing, foreign or incompatible profiles produce a fresh proposal without an
applied-profile reference. Anonymous block names require explicit drawing
aliases, even when their literal names happen to match. Multiple matching
aliases refuse automatic application. Aliases never identify a customer.

`test_x07_profile_concurrency.py` uses independent connections and barriers,
without sleeps. `test_x07_profile_storage_failures.py` injects post-write faults,
uses a 20 ms SQLite busy timeout for its contention fixture, and restores only
its own disposable corrupt file. It checks rollback, prior-record preservation
and retry after recovery. No live storage or credentials are involved.
