# Solar collapsed-tool layout check

Parent: Solar editing in Leaf's shared CAD workspace. This focused browser
fixture checks the collapsed tool launcher against the drawing, Objects
disclosure and compass areas observed in S01 readiness. It uses the shipped
cockpit and Solar CSS with synthetic drawing/control rectangles. It does not
open an app, authenticate, access a project or claim a LIVE layout proof.

Run from an owned checkout through the managed runner:

```powershell
python C:/Users/ehaug/.claude/scripts/mini_node.py run --worktree <checkout> --kind node --package-dir web --queue-wait-s 30 -- node e2e/solar-layout/seat-fit.mjs
```

With an already prepared local browser toolchain, the direct replay from web is:

```text
node e2e/solar-layout/seat-fit.mjs --out <evidence-directory>
```

The fixture measures open and collapsed states at1024,1280,1440,768 and390pixels.
Desktop collapsed cases must avoid the three synthetic obstacles and remain
inside the viewport. The report retains every rectangle so open and narrow
states can be compared before and after a CSS correction. Optional screenshots
and result.json remain durable after every page and the browser close.

To check that the correction preserves open and narrow placement, retain the
before-change result.json, then run with both --out <after-directory> and
--baseline <before-directory>/result.json. All seven open/narrow rectangles must
match exactly, including the1440pixel width override. The three collapsed
desktop cases must now avoid the drawing, Objects disclosure and compass.

The correction parks only the collapsed desktop launcher at the right canvas
edge below the compass. Expanded forms keep their existing bounded position,
scrolling and focus behavior. Phone and tablet rules are outside its media query.

All network requests are aborted. No private drawing or evaluator material is
included. The250pixel measured header boundary and drawing rectangle reproduce
the readiness layout's containing geometry, not a frozen evaluator target.
The real production CSS computes placement. A later changed-LIVE screenshot is
still needed to verify the corrected product presentation.
