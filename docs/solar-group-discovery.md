# Discover groups from a project drawing

Parent: the Solar CAD project workflow, using the existing `solar-panel-groups`
tool and canonical project version rail.

The tool accepts either manual `groups` or an explicit `discover` request. Do not
send both. Discovery reads the pinned version's source intake through the trusted
resolver. Callers supply drawing properties, never panel handles or memberships:

```json
{
  "expected_rev": 3,
  "discover": {
    "BranchMaxOffset": 1,
    "AlignmentTolerance": 0.5,
    "InstallationDesign": "Roof"
  }
}
```

These numbers are illustrative. Use the drawing's own recorded properties and
units. `BranchMaxOffset` retains the existing kernel's grown-outline semantics;
it is not a panel gap. `InstallationDesign` must match the project.

Discovery verifies source handles, centres and angles against the graph, requires
all recognised source panels to be represented, and partitions ungrouped panels
by connected roof island and angle. Singleton islands remain groups. Existing
groups stay intact. Names are deterministic from source handles. Matrices use
the existing cumulative graph budget and standard commit validation.

The existing sizing requirement still applies. This change does not provide an
electrical recommendation or alter stringing, sizing or module-power rules.
Canonical jobs retain the project, drawing, version, checkout and replay fences.
The specialised grouping form needs to send `discover` to select this mode;
manual `groups` keep their existing meaning.

Focused verification:

```text
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_solar_tool_solar_panel_groups.py server/tests/test_sip_r3e_groups.py -q
```

The plugin exclusion avoids an unrelated installed console plugin on Windows.
Tests use synthetic geometry and canonical store doubles. Live behavior still
requires a deployed source receipt and a normal project walk.
