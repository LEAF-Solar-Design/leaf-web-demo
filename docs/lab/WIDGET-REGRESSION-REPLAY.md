# Widget editor browser regression and reuse

Parent: Studio18 L01 Lab widget capability. The original independent A5 accepted
proof remains immutable. This suite adds repeatable browser regression evidence;
it does not rerun the benchmark or establish production availability.

## Run

From `web`, with the repository dependencies and Playwright Chromium installed:

```sh
npx playwright test --config playwright.widgets.config.mjs
```

The config builds a dedicated production bundle of the real WidgetsView and its
client. It owns a loopback preview server on port 5197, refuses an occupied port,
and lets Playwright close that server and every test browser context. It does not
read an environment file, reuse a user browser, require credentials, start the
full application or connect to a deployed document service. Artifact output is
`artifacts/widget-regression`. The harness is not part of the application bundle.

## Evidence and replay

The browser creates a blank project-scoped widget, adds literal synthetic sample
values (1000 to 1010 means +10 and +1%, with 3 open positions), leaves default
slate styling, performs actual pointer move and resize, and checks exact geometry
through Undo and Redo. It downloads and parses the browser's actual file, saves,
reloads, and compares the reopened export structurally. It checks that a missing
slate role uses the light host foreground and that publication stays absent.
The main test attaches the actual definition, request list, elapsed journey time
and preview screenshot. Timings are observations, not a performance guarantee.

Separate tests return 409 and 503 from the synthetic document service. Both keep
local edits and the previous stored revision. A conflict requires explicit Reload;
a temporary service error supports Save retry. Export remains blocked while the
server error is unresolved, preserving the existing product policy. Two fixture
projects using the same document name store separate artifacts and restore the
right project through the normal editor client.

These are real Chromium, production-built editor, pointer, Blob download and
client requests with a fixture API. The fixture is deliberately not authentication,
authorization, server durability, shared-profile navigation or LIVE proof. Other
benches may reuse the test's document-store fixture and normal editor controls,
but must retain that evidence label and supply their own host/service proof.

The isolated fixture defines only the relevant host foreground, ink and ground
roles; data-series-5 is absent to reproduce the original readable-default seam.
The fixture project selector is support setup, not the application's project UI.
No provider, account, drawing, trading, publication or phone state is involved.
