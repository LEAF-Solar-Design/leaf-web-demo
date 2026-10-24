# assistant-react: the Assistant Kit's web components

React components that render a `ThreadV1` from `packages/assistant-contract`
to the VI.3 UX standard (Part VI, K5). The contract is consumed by relative
path; consumers vendor a sha256-pinned copy of both packages per K-D1.

INTERNAL ONLY, never publish.

## Vendoring

The package is self-contained apart from the sibling `assistant-contract`.
Vendor both packages beside each other. `src/leaf-tokens.v1.json` is a
package-local copy of `contract/leaf-tokens.v1.json`; `test/self-contained.test.ts`
keeps them equal. React 18.3 and 19 are supported. From the Kit repo root,
`node tools/verify-km6.mjs` proves the React 19 consumer build.

## Components

The names are fixed by the standard and match the Swift side.

- `AssistantThread`: the whole surface. Provider and quota, items in seq order
  with blocks where they happened, the live turn's state with its one exit, and
  the composer. `availability` (`unconfigured`, `unpaired`, `access_revoked`,
  `assistant_disabled`, `update_required`) replaces the transcript;
  `unconfigured` keeps `assistant.empty` and `assistant.prompt` on screen.
- `AssistantComposer`: the counter starts at 7,000 (the TurnRequestV1 limit).
  Sends are optimistic: Sending, then Sent, or Not sent with the reason and the
  draft put back. A retry of the same text reuses its `request_id`.
- `AssistantStream`: `role=log`, `aria-live=polite`, `aria-busy` while a turn
  streams, one announcement on done.
- `AssistantTurnBubble`: one item. Model text is one plain text node, with no
  headings, links, buttons or checkmark glyphs, and no bidi overrides.
- `AssistantToolCard`: the ChipRow. Consecutive tool events merge into one
  line; a failed read says Unavailable and shows its reason.
- `AssistantViewPanel`: the fixed ANSWER frame (not a card, no StateInk).
  `metric` figures in Geist Mono, `table` bounded to 10 rows until expanded,
  `line` in the series palette with an accessibility summary. A null cell is
  the dash glyph labelled "Not reported".
- `AssistantConfirmCard` and its presets `ProposalCard`, `ConfigDraftCard` and
  `ArtifactCard` (`ConsentCard` picks one by kind). Above Approve it renders
  only server-origin fields; everything else, model prose above all, sits in
  the collapsed "Assistant's note". StateInk NEEDS YOUR OK with its glyph, an
  expiry countdown, the server's button labels, "Nothing was spent" only when
  the server says so, and the receipt line left in the transcript.
- `ReasonBanner`: the client authors text only for `client.offline` and
  `client.timeout`; a 429 shows its reset time in CT.
- `QuotaStrip`, `ProviderChip`, `StateCard` and the presets `ExpiredCard`,
  `ConsumedCard`, `WorkerOfflineCard`, `UpdateRequiredCard`.

## Native datasets (separate entry)

Import dataset rendering from the relative `src/datasets` entry, for example
`import { DatasetElement, resolveDisplayPreferences } from "./vendor/assistant-react/src/datasets"`.
It is intentionally absent from `src/index.ts`, so conversation consumers do
not load Recharts. Dataset hosts supply the `recharts` peer (`^3.6.0`); the
package also declares it for its own tests. Both React 18.3 and 19 are supported,
with no Next.js, Tailwind, host context, or host component requirement.

`DatasetElement` accepts S7's `element`, `read`, `preferences`, optional
`onRetry`, `onLoadMore(cursor)`, and `isLoading` props. Inputs are validated with
the sibling contract before display, including the dataset id/version/digest
and requested columns/axes. It never fetches, persists, or authorizes. Hosts
own those operations and set `isLoading` while callbacks are pending; retry
and pagination are disabled during loading. Failed or forbidden reads never
display retained rows. Stale/partial reads retain validated data with explicit
status and separately labelled source, synchronization, and fetch times.

Formatting exports are `DisplayPreferencesSchema`, `DisplayPreferencesError`,
`resolveDisplayPreferences(user?, workspace?)`, and `formatDatasetValue`.
Preferences resolve per field from user, workspace, then en-US/UTC/24h.
Date-only values keep their reporting day; instants use the viewer's timezone.
Money respects declared scale and currency fraction digits without FX, and
ratios respect their explicit scale, including negative and over-100% values.
For pure formatting without chart imports, use `src/datasets/formatting`.

Hosts may also wrap `LineChartCore` or `DataTableCore` in their own cards.
The chart retains categorical X by default and supports axis, formatter,
reference-line, and accessibility options. Dataset lines use the exported
pure `projectDatasetLine` helper for sorted quantitative positions, preserving
actual numeric/temporal intervals and null Y gaps. Quantitative coordinates
use JavaScript `Number` precision: instant fractions are added to whole-second
epoch milliseconds without `Date.parse` truncation, but distinguishable spacing
depends on the epoch's magnitude; arbitrary nanosecond precision across the full
date range is not guaranteed. The equivalent table retains the original ISO
strings in its source rows and formats them using the display preferences.
Null X stays in the
equivalent table and is omitted from the plot. `formatDatasetAxisValue` guards
generated temporal ticks outside the canonical range. Count axes use integer
ticks. Animations are disabled; the Recharts accessibility layer is enabled.

Tables use semantic headers and stable row keys in a keyboard-focusable
overflow region. `maxRows` bounds the visible viewport (default 12); all
supplied rows remain reachable by scrolling. Dataset query/row limits bound
the displayed data without mutating the authoritative read or saved query.
Line charts include a native “View data table” disclosure of those same rows.
The cores accept `className`/`style` for layout, use `--leaf-*` tokens with
host CSS-variable and package-token fallbacks, and fit their parent without
global CSS. Provider strings render as text, never raw HTML.

## States

`src/states.ts` holds every VI.3 state as data: its copy and exactly one exit.
`foldEvents` folds a ThreadEventV1 stream into the items and the live turn;
`liveStateFor` names the live turn's state.

## Tokens

Colours, fonts and the 44 px target come from `src/leaf-tokens.v1.json`,
the byte-equal package-local copy of `contract/leaf-tokens.v1.json`.
`tokenStyle("light" | "dark")` resolves shared colour roles, fonts, spacing and
radii from `profiles.mushy`; legacy-only colour keys retain their pinned values.
The existing `LEAF_TOKENS`, `color`, `font`, `radius` and `SERIES` exports stay
available. `AssistantRoot` sets resolved `--leaf-*` custom properties, including
the existing assistant variable names; components reference
`var(--leaf-*)` only, and a test fails any component that carries a literal
colour. Reduced motion is honoured in CSS and in the stream's scrolling.

## Verify

```
node tools/verify-k5.mjs
```

It installs and tests this package (vitest, jsdom, every component rendered
from the K0 golden conversations), then installs and builds
`examples/assistant-next` and checks the bundle.
