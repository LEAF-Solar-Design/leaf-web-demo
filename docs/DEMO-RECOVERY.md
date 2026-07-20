# Demo recovery playbook — what to do when it breaks on stage

One page. One section per failure mode you can actually hit during the golden
path (`docs/DEMO-GOLDEN-PATH.md`). Each has **one move** — the thing you do
while still talking. Do not debug on stage; recover and keep the beat.

**Before the call:** run the pre-flight and pre-warm two tabs.

```
python scripts/demo-preflight.py --offline      # must print PREFLIGHT: READY
```

If it prints `PREFLIGHT: NOT-READY <reason>`, fix that before the call — it is
the only signal that says the demo will actually produce the runbook numbers.
Backup of last resort: `deploy/presenter-flipbook.html` (double-click, works
with no network at all).

---

## Blank white screen

**Move:** you should see the calm dark card, not a white page — click **Reload**
on it. If the page really is white, switch to the second pre-warmed tab.

The app is wrapped in an ErrorBoundary (M4), so a mid-demo throw renders a
dark-emerald "Something went wrong" card with a Reload button instead of a white
screen. Say: "let me pop that back up" — reload, land back in mock mode, retype
the beat's prompt. Never open devtools on stage.

## Slow cold-load

**Move:** never hard-reload on stage — wait; the tab you pre-warmed is already
past this.

A cold load pays for the bundle and the sample intake once. That is exactly why
beat 0 is "switch to the tab you pre-warmed **before** the call". If you are on
conference wifi and the first load is crawling, tether to a phone hotspot and
load the reserve tab there while you keep talking. If both are slow, go to the
flipbook rather than watching a spinner.

## 401 / sign-in wall

**Move:** say nothing and keep going — you are already in the demo.

The demo runs in mock mode: no backend, no Auth0, no APS. If a live-mode request
ever answers 401, the app auto-falls back to the mock path rather than showing a
sign-in wall. If you somehow land on the "not signed in" gate, the Mock checkbox
in the header puts you back in one click. Do not attempt to sign in on stage.

## WebGL unavailable

**Move:** you are on the wrong browser — switch to the GPU browser profile you
pre-warmed.

If the viewer shows the "WebGL unavailable" text instead of the rooftop, the
browser has no hardware GL context (headless/remote-desktop sessions and some
locked-down VDI profiles do this). Use a real GPU browser window on the local
machine. The numbers still come out on the results side, but the drawing is the
picture people remember — if you cannot get GL, run beat 0 from the flipbook.

## Mock toggle accidentally unchecked

**Move:** re-check the **Mock** checkbox in the header.

An accidental click on the toggle drops you into live mode, where results stop
matching the runbook. Re-check it; the mock registry and sample rooftop come
straight back. Re-run the current beat's prompt so the visible result matches
what you just said out loud.

## A run shows a raw error

**Move:** errors are humanized — read the calm sentence aloud, open **Details**
only if you need the job id.

Error strings are passed through the humanizer (M4), so the surface never shows
`-> 502` or `/api/...` on stage. The technical text and the job id live behind
the **Details** disclosure — quote that id if a prospect asks what they would
send support. Then move to the next beat; do not retry the same prompt twice.

## Numbers on screen disagree with the runbook

**Move:** you are not in mock mode, or you are not on the sample rooftop — check
the Mock checkbox first.

The golden numbers (2345 panels, 48,718 sqft, 72 near-edge at 60 in) are
recomputed from `web/public/sample.intake.json` by both
`web/test/check_integration.mjs` and `scripts/demo-preflight.py`. If the running
app disagrees, the app is reading something else — it is not that the numbers
drifted.

## Total loss (no laptop, no network, no app)

**Move:** open `deploy/presenter-flipbook.html` by double-click and walk the
beats from it.

It is self-contained — no server, no external references, no network — and it
carries the same beats and the same numbers as the runbook. Say "here is the run
we did this morning" and keep the conversation on the product.
