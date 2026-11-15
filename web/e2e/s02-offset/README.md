# Native numeric read input regression

This local browser fixture exercises the real project Solar read form with an
inert catalog and transport. It uses no account, saved project, model call or
external API. It does not establish LIVE, PostgreSQL or full-App readiness.

From `web`, with an installed Playwright Chromium:

```sh
node e2e/s02-offset/offset-input.mjs --out /absolute/evidence/path
```

Use `--browser-executable /path/to/chromium` for an existing compatible browser.
Use `--source-root /path/to/web` to run the same probe against a predecessor.
No browser or dependency download is performed by the script.

The fixture fills Offset before and after a completed read, checks the actual
transport receives the edited numbers, and tests ordinary keyboard entry. It
also requests submission during native input capture: that stale submission
must be refused, while a subsequent ordinary read sends the new value.

Capture must retire pending requests and parameter revisions synchronously.
It must not render the previous controlled value before React processes the
change callback. The normal change callback commits the new form state.

`receipt.json` records the source file digest, result and resource closure.
`offset-kept.png` is local rendered evidence. On Windows, canonical source,
working and cache paths avoid drive-alias mismatches in Vite/esbuild metadata.
The browser and fixture server close in `finally` on both pass and failure.
