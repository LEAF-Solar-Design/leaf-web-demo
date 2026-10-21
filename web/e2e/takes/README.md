# Studio takes

Beat files in `beats/*.json` are portable data for both the standalone
Playwright take and a Python runner using one persistent headed Edge context.
They contain no CSS selectors, executable expressions, or browser scripts.

`SW1ZI` (zoom in), `SW1ZO` (zoom out), and `SW1FIT` (fit drawing to view)
are motion beats that run after the SW1 pre-roll, with the drawing already open.
Each zoom beat clicks five times; every click is followed by a 300 ms pause.

## `leaf.studio-beats.v1`

All fields below are required; unknown fields are rejected:

| Field | Value |
| --- | --- |
| `schema` | `"leaf.studio-beats.v1"` |
| `beat_id` | Nonempty beat identifier (`"SW1"` for split-guest-drawing-open) |
| `env` | `"staging"` |
| `origin` | `"https://platform-staging.leafdesign.ai"` |
| `requires_signed_in` | Boolean; the caller supplies the appropriate context |
| `fixtures` | Array of `{path, sha256, bytes}`; paths are repo-relative POSIX paths, SHA-256 is lowercase hex, bytes is a nonnegative integer |
| `steps` | Nonempty ordered array of the step objects below |
| `expect_text` | The `text` values of every `assert_text` step, in order |

Each step has a `verb` and exactly the associated fields. These are the only
ten verbs:

| `verb` | Other fields | Meaning |
| --- | --- | --- |
| `goto` | `path` | Navigate to an origin-relative path, including any query |
| `click` | `testid` OR `role` + `name` OR `text` | Click an exact target |
| `fill` | `target`, `value` | Fill a form control with a string |
| `upload` | `target`, `fixture` | Set a file input to the declared fixture's repo-relative path |
| `press` | `key` | Press a Playwright keyboard key or chord on the focused element |
| `wait_text` | `text`, `timeout_ms` | Wait for an exact visible text match; positive integer timeout |
| `assert_text` | `text` | Assert an exact visible text match (30-second retry budget) |
| `scroll_to` | `target` | Scroll the target into view |
| `hover` | `target` | Hover over the target |
| `pause` | `ms` | Hold for a nonnegative integer number of milliseconds |

Targets are exactly one of `{testid}`, `{role, name}`, or `{text}`. Prefer
existing test IDs, then an exact accessible role and name, then exact text.
For `fill` and `upload`, a text target names the control's exact accessible
label (`getByLabel`); for other actions it matches exact rendered text
(`getByText`). File inputs have no implicit ARIA role, so SW1 uses
`{"text":"DXF file"}` after clicking the document-tab `+` button with role
`button` and accessible name `Open a DXF in the browser engine`. The quick-access
`Open DXF` action is unavailable in this signed-out build. No new product test
IDs are needed. Action targets must resolve unambiguously;
text assertions allow repeated text but require at least one visible match.

`runBeat(page, beat, params = {})` validates the whole beat before executing
any step. It uses the page/context's configured base URL for `goto`; callers
must configure the beat's declared origin. Fixture paths resolve from the
repository root, independently of the working directory. Absolute paths,
parent components and symlinks outside the repository are refused. Before each
upload, the runner checks the declared byte count and SHA-256 and sends those
verified bytes to the file input.
The caller owns sign-in and context lifetime; the runner never resets a shared
context. It checks `window.innerWidth >= 981` before and after the steps.
Its only browser evaluation reads that width; no step can evaluate scripts.
Step failures include the beat ID, zero-based `steps[index]`, verb and original
error message, with the original error retained as `cause`. Playback stops there.

The returned record is
`{beat_id, sync_id, t_start_ms, t_end_ms, expect_text, inner_width}`.
The split plan supplies `params.sync_id`; the runner echoes it unchanged,
defaulting to `null`. Times use Node's `performance.timeOrigin +
performance.now()` (milliseconds), so navigation cannot reset the beat clock.
`inner_width` is the final measured browser width.

## Standalone SW1 take

The wrapper uses a fresh signed-out context, a 1600×1000 viewport and video
at that size. It also checks all declared assertion strings inside the
Properties panel and writes exactly one record to `SW1.beat.jsonl` in the
test's output directory, attaching that file to the report. The opening and
closing pauses leave time for video capture around the visible actions.

SW1 leaves the import pane open after loading; Properties is independently
visible, so a second opener click is unnecessary. It waits for the loaded-file
status and `browser drawing` source before asserting the uploaded drawing's
facts. Its Drawing rows must show `Layers` = `1` and `Layers shown` = `1`;
the wrapper checks those label/value pairs together. The engine view derives
layers from entity layer names: all 2,345 fixture entities are on `Panels`.
The four layer-table entries in the preloaded sample are not the browser
drawing's layer count. Staging playback remains the planner's verification.

From the repository root:

```sh
node --test web/e2e/takes/beats.test.mjs
LEAF_TAKE_ORIGIN=https://platform-staging.leafdesign.ai node web/node_modules/@playwright/test/cli.js test -c web/playwright.config.mjs sw1-take
```

The take skips with a setup reason when `LEAF_TAKE_ORIGIN` is unset.
Set it to `https://platform-staging.leafdesign.ai` to run against the beat's
declared origin. Every other value (including an empty string) is rejected. The config's own
web-server lifecycle still applies. Optional `LEAF_TAKE_SYNC_ID` supplies the
standalone record's sync ID; a split runner passes its plan's ID directly.
Playback requires the app to serve its compiled browser CAD engine; the
fixture is explicitly opened in that engine, never replaced with mocked data.
