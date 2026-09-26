# CAD Job Timing Contract

Parent context: this contract supports the Leaf Platform CAD execution performance lane by making the authored drawing-write path measurable from source fetch through immutable version publication.

Successful authored writes store `leaf.cad-timing.v1` under
`execution_provenance.cad_timing` in the durable job result. The record keeps the
existing result-envelope keys unchanged.

## Spans

| Span | Meaning | Source |
| --- | --- | --- |
| `submit` | Local submission start until APS records the work item as queued | Local wall clock plus APS `timeQueued` |
| `queue` | APS queue wait | APS lifecycle timestamps |
| `task_start` | APS download start until instructions start | APS lifecycle timestamps |
| `image_pull` | Container image pull inside APS | Unavailable from the current APS status contract |
| `drawing_fetch` | Manifest, intake, and source drawing fetch before planning | Server monotonic clock |
| `planner` | Sandboxed authored planner execution before APS submission | Server monotonic clock |
| `engine` | AutoCAD instruction execution | APS lifecycle timestamps |
| `output_upload` | APS instruction end until output upload end | APS lifecycle timestamps |
| `output_inspection` | Download, parse, and effect verification of the intake produced by reopening the saved DWG inside the mutation WorkItem | Server monotonic clock |
| `version_write` | Immutable drawing version write | Server monotonic clock |
| `publish` | Intake-cache publication and read-back proof | Server monotonic clock |
| `client_delivery` | first terminal job-status read minus `finished_at`, from `client_delivered_at` in the job provenance | App wall clock |

Every unavailable measurement is `null` and is named in `unavailable_spans`.
Consumers must not treat a missing provider measurement as zero. `total_ms`
remains the existing server execution duration. `provider_accounted_ms` covers
the measured APS portion from local submit through APS output upload.

`GET /api/jobs/{job_id}` stamps the first authorized terminal CAD result read.
`POST /api/run?wait=1` also stamps when it returns a completed CAD result.
The timestamp is first-writer-wins in both job stores. `GET /api/jobs` lists
history without stamping, and `GET /api/jobs/{id}/stream` carries status without
a result and does not stamp. A negative difference caused by clock skew between
the finishing host and the app leaves `client_delivery` null. `image_pull`
remains unavailable.
