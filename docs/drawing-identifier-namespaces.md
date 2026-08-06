# Drawing identifier namespaces

Parent context: this map supports the Leaf Platform drawing execution and
versioning system. It records each identifier's role so UI, instant, batch,
APS, and storage lanes do not confuse names that happen to describe one
drawing.

| Namespace | Role | Bundled demo value | Authority |
| --- | --- | --- | --- |
| UI project id | Selects the containing project | UUID from `projects.project_id` | Platform database |
| Canonical drawing id | Selects a drawing artifact within the project | UUID from `drawing_artifacts.drawing_id` | Platform database |
| Drawing version id | Pins one immutable project input | UUID from `drawing_versions.version_id` | Platform database |
| Execution source id | Selects the broker-owned source file | `rooftop_demo` | Broker source registry |
| Version-store id | Selects the tenant-scoped version chain | `demo` | Drawing store manifest |
| Source storage key | Locates the curated DWG | `data/rooftop_demo.dwg` | Broker filesystem image |
| Version storage key | Locates one immutable stored version | `tenants/<tenant>/drawings/demo/v/<version>` | Drawing store backend |
| URL drawing id | Selects drawing routes | `demo`, with `rooftop_demo` compatibility | App drawing router |

`server/drawing_identity.py` owns the curated compatibility rule. The aliases
`rooftop_demo`, `rooftop-demo`, and `demo` share source id `rooftop_demo` and
tenant-store id `demo`. Unknown identifiers pass through for tenant-scoped,
fail-closed resolution. The alias rule grants no access and never searches a
different tenant.
