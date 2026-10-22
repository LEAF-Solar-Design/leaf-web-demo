# Widget builder: input to studio-engine-lab (mandate 2)

This is the operator's widget designer, merged into this lane on 2026-09-30. Operator (verbatim): "merge this work into the leaf studio fork lane, this will be one of the sub-tabs/elements, allowing users to build their own widgets"

## What it is

`kashbot-widget-designer-v2.html` is a single-file, drag-and-drop designer. It is live as a claude.ai artifact: https://claude.ai/artifact/TBsoVAMP53ULgfYsoPUUcs (v2, db capability; the source was built by session e8d4b219).

- **Formats (13):** Live Activity (lock screen, Island expanded, compact and minimal, StandBy), lock screen widgets (rectangular, circular, inline), home screen widgets (S/M/L), and Apple Watch (Smart Stack, complication).
- **Per format:** a true iOS point size, the device frame in context, the camera cutout for the Island formats, the 160 pt Live Activity limit, and a one-color preview for accessory widgets.
- **Elements (30)**, in six groups:
  - Headline
  - Open positions: per-row columns that align when they share a top and height
  - Charts: sparkline and ring gauge
  - History: strip, list and win/loss dots
  - Status: live dot, updated time, settle countdown and open count
  - Decoration
- **Editing:** drag, 8-handle resize, snapping with guides, undo and redo, duplicate, a layers panel and an inspector.
- **Data:** sample only, with live ticks. It is NOT wired to real data yet.

## Data model (what a saved design looks like)

```
doc = { v:2, active:"la", formats:{ <formatId>: { h, radius, bg:"black|blur|none", vibrant, ship, els:[Element] } } }
Element = { id, kind, x, y, w, h, scale, weight, color:"auto|ink|leaf|slate|loss", align, visible, ...kind props (arrows,count,maxRows,source,text,tracking,showPct,opacity) }
```

- Coordinates are iOS points within the format's surface. Format widths are fixed per `FORMATS`.
- A save writes to the artifact db at `layouts/kashbot-lock` with `{doc, export, note, savedAtMs}`, plus a copy at `versions/v<ms>`.

## What the app ships today (the "in app" formats)

This lives in leaf-hft `ios/` (#898, #934, #940, main 0314198e):

- `KashbotBoardViews.swift`, `KashbotLiveActivityWidget.swift`, `KashbotLockWidget.swift` and `KashbotLedgerWidget.swift`.
- The wire contract is `docs/kashbot-live-activity.md` (v1 plus additive `pnlCents`).
- The server push is ops-dashboard `kashbot-live-board.ts` and `kashbot-live-push.ts`.

## Suggestions for the lane (the planner decides)

- In Studio, the designer becomes a sub-tab: its source-of-truth doc moves to Studio's store, and its element catalog binds to the lab's real data routes instead of `sample`.
- For the phone to render a user's design, the widget needs a layout-driven renderer: a SwiftUI view that reads a doc like the one above from the app group or a push. That is a new contract. Today's views are hand-built per format.
- ActivityKit's content state is 4 KB, so the layout should ship through the app or widget config, not through every push.
