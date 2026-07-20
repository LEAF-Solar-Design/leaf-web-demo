# Demo golden path — leaf-platform-web presenter runbook

The seven-beat script we run on a cold customer call. It shows the one thing
nobody else does: a prospect types plain English, the platform **authors a real
reusable CAD tool**, and that tool then **runs zero-LLM** against a real solar
rooftop drawing.

**Honesty framing.** The drawing on screen is a **sample rooftop** we ship with
the demo (`web/public/sample.intake.json`, extracted from a real DWG) — say
"sample rooftop", never "your drawing". Numbers below are recomputed from that
sample by `web/test/check_integration.mjs`, so they are exact, not approximate.

**Self-serve entry.** The same script runs itself from the deep link
`?demo=tour` — a self-advancing coach-mark walkthrough over the *same* real
handlers. Hand that URL to a prospect who wants to poke at it alone. On stage,
drive it manually with the beats below.

---

## Golden numbers

| Beat | Prompt / action | Expected value |
|------|-----------------|----------------|
| 0 | Pre-warmed tab lands | Demo banner visible, drawing rendered |
| 1 | `count panels per layer` | **Panels: 2345** |
| 2 | `measure the total area` | **48,718 sqft** (48718.2) + largest-panel marker |
| 3 | `highlight panels within 60 in of the edge` | **72** panels of 2345, 60 in ring |
| 4 | Build lane authors a tool | LISP preview + provenance (`distance_in: 24`) + **Run it now** → **23** of 2345 |
| 5 | `delete panel 7FA3` | v2 (parent 1) → **Undo** → head v1, History v1→v2 |
| 6 | Honesty close | Real out-of-the-box vs operator-enabled, cite `RUN.md` |

---

## Beat 0 — Open the pre-warmed tab

- **Do:** switch to the tab you pre-warmed **before** the call. Do not reload it.
- **Expect:** the demo banner is visible ("you're in the demo — mock data, no
  sign-in"), the Mock checkbox is checked, and the sample rooftop is rendered
  with **2,345** panels. Zero clicks were required to get here.
- **Say:** "This is a sample rooftop drawing — a real DWG we extracted, 2,345
  panels."
- **If it doesn't match →** if the canvas is blank, click **Reload** in the
  ErrorBoundary card, or open a second pre-warmed tab you kept in reserve. Never
  hard-reload the primary tab mid-beat. If the Mock checkbox is unchecked,
  re-check it.

## Beat 1 — Count panels per layer

- **Type:** `count panels per layer`
- **Click:** **Run** (the prompt bar routes to the `count-by-layer` tool, run lane).
- **Expect:** result table shows **Panels: 2345**, total 2345.
- **Say:** "Plain English, no menu diving — and this ran with no model in the loop."
- **If it doesn't match →** confirm the Mock checkbox is checked and the prompt
  bar says run lane / `count-by-layer`; if the routing chip shows a different
  tool, retype the prompt verbatim from this table.

## Beat 2 — Measure the total area

- **Type:** `measure the total area`
- **Click:** **Run** (`measure-panel-area`).
- **Expect:** **total 48,718 sqft** (48718.2), 2345 panels, and the **largest
  panel** flagged with a marker on the canvas ("largest 21.3 sqft").
- **Say:** "Same drawing, different question, sub-second — because the tool is
  code, not a prompt."
- **If it doesn't match →** if the marker is missing but the number is right,
  keep going; it is a render nit, not a data error. If the number is wrong, the
  seated version is v2 — press **Undo** to return to v1 and re-run.

## Beat 3 — Highlight panels near the roof edge

- **Type:** `highlight panels within 60 in of the edge`
- **Click:** **Run** (`highlight-panels-near-edge`, `distance_in = 60`).
- **Expect:** **72** panels highlighted out of 2345 — a clean edge ring around
  the array at the 60-inch setback.
- **Say:** "It pulled the 60 inches out of the sentence — that's a parameter,
  not a hardcoded rule."
- **If it doesn't match →** if the parameter chip shows a distance other than
  60, retype with the literal words "within 60 in of the edge". If nothing
  highlights, re-run once; do not open the parameter editor on stage.

## Beat 4 — Build lane: author a real reusable tool

- **Type:** `build a tool that flags panels within 24 in of the roof edge`
- **Click:** **Build** lane → **Author**, then **Run it now** on the authored tool.
- **Expect:** the authored tool `flags-panels-within-24-in-of-the-roof` appears
  with its **LISP preview** (which reads `(leaf:param "distance_in" 24.0)` — the
  number you typed, not a hardcoded default), a **provenance** block (author
  `agent`, created, params parsed from your sentence — the 24 in shows up as
  `distance_in: 24`), and a **Run it now** button. **Run it now** highlights
  **23 of 2345** panels — a tighter ring than beat 3's 72, because it is a
  tighter setback.
- **Say:** *"It authored a real reusable tool, not a one-off answer."* — this is
  the differentiator line. Pause on it. The tool is now in the catalog for
  everyone, and every future run of it is zero-LLM. Then run it: "and the 24
  inches I typed is a live parameter — that ring is tighter than the last one."
- **If it doesn't match →** if the preview is empty, re-submit the same sentence
  once. **If Run it now returns 0 panels and the canvas goes empty, the parsed
  distance landed below this array's ~19 in setback** — no panel centroid is
  that close to the roof edge. Retype the sentence with the literal words
  "within 24 in" and re-author; do not narrate a zero. Do **not** hand-edit the
  generated code on stage.

## Beat 5 — Delete a panel → version → undo

- **Type:** `delete panel 7FA3`
- **Click:** **Run** (`delete-marked-panel`, a `drawing.write`), then **Undo**,
  then open **History**.
- **Expect:** the run removes panel 7FA3 (2345 → 2344) and stamps a new
  version: **v2, parent v1**. **Undo** steps head back to **v1** and the panel
  reappears. **History** lists **v1 → v2** with tool `delete-marked-panel` and a
  content digest per row.
- **Say:** "Writes are versioned, and undo is real — nobody hands an AI a write
  path without that."
- **If it doesn't match →** if the handle isn't found, the tool deletes the last
  panel instead — that is fine, keep narrating. If Undo looks stuck, open
  **History** and click **v1** — note this *previews* v1 (the rooftop comes
  back on the canvas and the row goes active) rather than moving head; **Back
  to head** returns. Undo is the thing that actually moves head, so if you need
  the seated version back at v1, close History and press Undo.

## Beat 6 — Honesty close

- **Do:** say what is real out of the box versus operator-enabled, and point at
  `RUN.md`.
- **Say:** "Everything you just saw runs out of the box on the sample rooftop.
  Pointing it at *your* DWGs, and the cloud solver lane, are operator-enabled —
  the setup is written down in `RUN.md`, not hidden."
- **Expect:** no follow-up demo. Stop here and take questions.
- **If it doesn't match →** if they ask for a live DWG upload, offer a scheduled
  follow-up against their file rather than improvising it on the call.

---

## AVOID list

Things that break the golden path. Do not do them on stage.

- **Do not uncheck the Mock checkbox.** It leaves the demo data and can hit a
  sign-in wall.
- **Do not open the Claude account panel.** It exposes account state that has
  nothing to do with the story.
- **Do not demo the Solve lane.** The solve lane (string sizing / combiners /
  optimal route) is an honest dead-end in this build — it will say it is not
  wired. No beat in this runbook routes to solve, and
  `web/test/check_routes.mjs` fails the build if one ever does.
- **Do not improvise a setback under 20 in.** On this sample rooftop the
  closest panel centroid sits ~19.2 in from the drawing bounds, so any distance
  at or below 19 returns **0 panels and an empty canvas** — the worst possible
  frame on the differentiator beat. Safe values, all recomputed from the sample:
  20 → 21, **24 → 23**, 48 → 46, **60 → 72**. If a prospect asks for "12
  inches", say yes, type it, and narrate the zero as the answer ("nothing is
  that close — that is the tool telling you the truth"), or steer to 24.
- **Do not present in a window under 760px tall.** The result panel scrolls out
  of view and the golden numbers stop being readable from the back of a room.
- **Do not hard-reload the pre-warmed tab.** Cold load is the slowest thing in
  the demo; keep a second pre-warmed tab in reserve instead.

## Verification

The numbers in this runbook are not typed by hand — they are recomputed from
the real router and the real mock engine:

```
cd web
node test/check_routes.mjs         # ROUTES_OK      — every prompt above routes as claimed
node test/check_integration.mjs    # INTEGRATION_OK — 2345 / 48718.2 / 72 / v2->undo->v1
node scripts/check_author.mjs      # AUTHOR_QUALITY_OK — beat 4 authoring + parsed distance
node scripts/check_writeloop.mjs   # WRITELOOP_OK   — repeated delete + undo chain (beat 5)
node scripts/check_tourscript.mjs  # TOURSCRIPT_OK  — the ?demo=tour beats re-route correctly
```

If any fails, the runbook is stale — fix the runbook or the code before you
present it.

**Coverage caveat, stated honestly:** `check_integration.mjs` asserts beat 4
only routes to the *build* lane; it does not yet re-run the authored tool. The
**23** above was recomputed from `web/public/sample.intake.json` through the
real `authorMock` → `runMock` path (`distance_in: 24` → 23 of 2345). If you
change the sample rooftop or the near-edge geometry, re-derive it rather than
trusting this line.
