# leaf-platform-webview: the palette opens a page no live surface serves

**Plugin:** Branch2025 `WebBridge/LeafPlatformWebViewHost.cs` 424-438 (`TryPlatformOrigin`: with
`LEAF_PLATFORM_ORIGIN` unset the origin is `https://app.leafdesign.ai` and the page is
`/app/leaf-platform?host=autocad`), 169-176 and 395-398 (the palette only navigates within that origin and the
Auth0 domain), 267-320 (the one drawing write, a binding record, needs a signed binding from the page and a Yes in
AutoCAD).
**Proven:** licensed AutoCAD 2025 on the VMC, release build 1.1.4.4, 2026-09-24, step p1 (LEAFPLATFORM in the shipped
configuration). The palette showed WebView2's "server IP address could not be found" page for app.leafdesign.ai.
Saved and reopened, the drawing did not change: no entity, dictionary or setting differs from its source.

## Why

- app.leafdesign.ai has no DNS record (non-existent domain from 1.1.1.1, 2026-09-24).
- The other hosts the palette accepts do not serve its page either. platform.leafdesign.ai serves the Studio bundle,
  which carries no host bridge (none of its script chunks posts or receives WebView2 messages, checked 2026-09-24).
  The website's `/app/leaf-platform` route was retired to `/try` on 2026-07-26, and `/try` forwards to
  studio.leafautomation.ai (operator decision 2026-09-16). With `LEAF_PLATFORM_ORIGIN` pointed at a local copy of the
  website, the palette followed that redirect and refused it: "Blocked navigation outside the Leaf Platform sign-in
  boundary."

So in every configuration the plugin ships or allows, the palette cannot show the platform. Its bind path is
unreachable and it commits nothing.

## Studio's behaviour, and the declared diffs

Studio is the hosted authoring surface the palette was built to open. It has no host palette, and opening it writes
nothing to the drawing (`server/solar_batch2_simple.py`, `platform_webview_rows`). Both sides commit nothing. The
declared diffs are exactly the comparator's for the plugin's two `report` rows (`platform-page` unreachable, `error`
name-not-resolved): the `after/rows` and `entity_mapping` lengths.

## Retiring it

The divergence goes away when the palette opens a page that serves the host bridge: the plugin's default origin points
at a live host whose `/app/leaf-platform?host=autocad` serves it, or LEAFPLATFORM leaves the plugin. Either is a
hosting or shipped-plugin decision for the operator.
