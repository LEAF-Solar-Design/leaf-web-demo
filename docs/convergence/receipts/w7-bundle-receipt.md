# W7 bundle receipt (criterion 3)

The W7-C deletion PR #1421 merged as 72a288f0 on 2026-09-24. Its build is compared with the build immediately before it, b2c801a1 (#1422), for Version 3 criterion 3 in ACCEPTANCE.md.

The planner measured both builds on mac-mini on 2026-10-16 through scripts/mini_node.py, kind node, running `npm --prefix web run build` then `node web/scripts/check_bundle_budget.mjs`. The measurements are the check_bundle_budget.mjs output, using gzip -9 (level 9) per chunk. The deletion build used Vite 6.4.3 and 349 modules. Both builds used the then-current baseline of 507,842 gz bytes (_ref 0e6ffdaf) and ceiling of 524,226, and both reported "bundle budget ok".

The deletion build has no runtime flag because W7 removed web/public/runtime-flags.js. The predecessor was built with its committed defaults. Neither build rewrote bundle-baseline.json.

## Deletion build: 72a288f0 (W7-C, #1421)

| Chunk | gz bytes | Delta vs baseline |
| --- | ---: | ---: |
| App.css | 7,099 | -9 |
| App.js | 78,797 | +1,016 |
| ElementContextMenu.js | 34,202 | -51 |
| SheetsPage.css | 2,583 | -2 |
| SheetsPage.js | 5,421 | +20 |
| Viewer.js | 12,464 | +588 |
| index.css | 36,292 | +13 |
| index.js | 156,552 | +247 |
| useAnnotations.js | 2,006 | -3 |
| vendor-viewer.js | 169,688 | -894 |
| worker-browser.js | 3,663 | +0 |

## Predecessor build: b2c801a1 (#1422)

| Chunk | gz bytes | Delta vs baseline |
| --- | ---: | ---: |
| App.css | 7,099 | -9 |
| App.js | 78,794 | +1,013 |
| ElementContextMenu.js | 34,203 | -50 |
| SheetsPage.css | 2,583 | -2 |
| SheetsPage.js | 5,421 | +20 |
| Viewer.js | 12,464 | +588 |
| index.css | 36,276 | -3 |
| index.js | 156,595 | +290 |
| useAnnotations.js | 2,005 | -4 |
| vendor-viewer.js | 169,688 | -894 |
| worker-browser.js | 3,663 | +0 |

## Totals

All totals and deltas are gzip bytes. The W0 freeze is 337,890 bytes; the then-current baseline is 507,842 bytes. The predecessor row is the reference for the deletion comparison, so its own predecessor delta is not measured here.

| Build | SHA | TOTAL | vs predecessor | vs W0 | vs baseline |
| --- | --- | ---: | ---: | ---: | ---: |
| Deletion (W7-C, #1421) | 72a288f0 | 508,767 | -24 | +170,877 | +925 |
| Predecessor (#1422) | b2c801a1 | 508,791 | Not measured | +170,901 | +949 |

Verdict: criterion 3 met. The deletion total is 24 bytes below the immediately preceding build.
