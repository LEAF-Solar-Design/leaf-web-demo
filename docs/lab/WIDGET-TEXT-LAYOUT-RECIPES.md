# Widget text layout recipes

Use these recipes in the Leaf workspace's Lab widget editor. Keep the same
project identity when you change workspace profiles. The
[authoring contract](WIDGET-AUTHORING-CONTRACT.md) explains persistence and host inputs.

## Stored text and visible text

The short Text field accepts one line and 40 UTF-16 units. Choose **Edit full
text** for up to 512 UTF-16 units, including newlines. Loaded long or multiline
values open in Full text. Save and Download preserve the stored text.

The preview clips text to its element box. It emits one SVG text node per plain
text element. A stored newline does not create another visual row. Viewport
scaling does not change stored geometry or fit the text automatically.

For plain text, font size is `H * 0.8 * Scale`. Increasing H can therefore make
a line wider. Increase W or reduce Scale when text clips. Inspect the result
with the actual host font, weight and colors.

## Starting layouts

These values use the internal lock-screen board at 361 by 160 logical points.
Use X 14, W 330, H 24 and Color ink as a starting point. Confirm that the host gives
ink enough contrast against the chosen background. Geometry alone does not prove
glyph fit or native device publication readiness.

| Text | Editor and starting values | What to check |
|---|---|---|
| Short: `Open: 3` | Text, Y 14, Scale 1 | The full label is visible inside the box. |
| Long: `Review the generation summary for this project` | Full text, Y 14, Scale 0.66 | Inspect both ends of the line. Shorten the label or reduce Scale if needed. |
| Multiline: `Line one` followed by a newline and `Line two` | Full text retains one value. For two visible rows, add two elements at Y 14 and Y 46, each with H 24 and Scale 1. | Compare the downloaded text to the intended data. Inspect the two visible rows separately. |
| Combining marks and emoji: `é 😀` | Text for a short value, Full text for a longer one | A base letter plus a combining mark uses two units. The emoji uses two units. Visual width depends on the font. |

The proved four-row preview recipe uses Y 14, Y 46, Y 78 and Y 110, each at X14,
W330 and H24. Its last row ends at 134 within this internal board of 160 points.
Check the separate content-height limit before any native publication.

## Replay and evidence

Run these commands inside `web` through the configured test runner:

- `npx playwright test --config playwright.widget-unicode.config.mjs` checks exact multiline, emoji, combining-mark and escape-character Save/Load/Blob data.
- `npx playwright test --config playwright.widget-boundary.config.mjs` checks native insertion at the 512-unit boundary in the tested Chromium browser.
- `npx playwright test --config playwright.widget-zoom.config.mjs` checks keyboard access and exact export at 800 by 600 CSS pixels with pixel ratio 2. This is enlarged reflow, not a browser zoom-menu assertion.

The [Unicode case](../../web/e2e/widgets/widget-unicode.case.mjs),
[boundary case](../../web/e2e/widgets/widget-text-boundary.case.mjs) and
[reflow case](../../web/e2e/widgets/widget-zoom.case.mjs) save their own proof.
These fixtures use synthetic data and intercepted document endpoints. They do
not prove deployed service access. The ordinary native input child selects
three input cases; the standalone reflow command selects its own case.
