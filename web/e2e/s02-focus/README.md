# Saved tracking focus replay

This tests the actual read/inspection components with synthetic transport in a
local Vite server. It uses no account, live project, model, or external API.
It does not prove the full App, real entitlements, database, or staging.

From `web`, use an installed Chromium without downloading a browser:

```text
node e2e/s02-focus/inspection-focus.mjs --case keyboard --out <new absolute evidence directory> --browser-executable <installed Chromium executable>
node e2e/s02-focus/inspection-focus.mjs --case response-order --out <another new absolute evidence directory> --browser-executable <installed Chromium executable>
```

Keyboard mode starts at1440x900, uses Tab/Enter through the actual controls and
checks that focus moves to Previous when Next becomes disabled. The existing
polite page-range region must change. It also records a390x844 overflow check.
The inherited `focus_after_shift_tab` receipt field records Previous focus;
the corrected path needs no Shift+Tab recovery.

Response mode deliberately ignores abort signals in the transport. It verifies
late page/version responses cannot replace current output. A failed read must
return focus to the enabled Read button, and Enter must retry successfully.
Focus on a different control is never reclaimed by a late result.

The runner writes a receipt and screenshots, closes its dedicated browser and
Vite server in `finally`, and records failures rather than treating them as PASS.
DOM live-region changes are observed, not actual screen-reader speech. No video
or production data is included. `--source-root` may select a predecessor web
directory for a before-control run; it must have the same local dependencies.
