# S02 text widget preview — slice A

This isolated component is the first text-element port for the Leaf Automation
Studio Lab widget-designer mandate. S02 still needs shared Studio sub-tab
integration and source-bound browser proof. This slice does not deliver a
shared-shell mount, hosted availability, publication, persistence or native
rendering. The hosted-audience choice remains with the parent owner.

## Provenance and adaptation

Read-only inputs supplied for this port:

- `.pair-ref/kashbot-widget-designer-v2.html`, SHA-256
  `587d48f05b2164677438be7e4d82c39f244cac9a92950d53999997f086129639`
- `.pair-ref/README.md`, SHA-256
  `89ad68d3d09b50793033660a46a97963503515544fa9b9cd58d141f8def1bc46`

The original source was supplied from
`C:/Users/ehaug/.claude/program/studio-engine-lab-20260930/inputs/widget-builder/`.
The port follows the text catalog entry, `mk` helper, text render branch and
inspector controls. It keeps the Lock screen's 361 × 160 point geometry and
the text defaults: width 120, height 16, scale 1, weight 500, slate color,
left alignment and visible state. Font size follows `height * 0.8 * scale`.
New elements start at (16, 16), inset from the rounded preview corners so the
first letters stay visible. Default copy is `Your text`.

Adaptations: positions must keep the full box inside the canvas (default
X range 0–241, Y range 0–144). Text is limited to 512 characters rather than
the original inspector's 40; the draft has at most 32 elements. The preview
shrinks proportionally at narrow widths. Text stays on one line, clipped to
its box; this is a visual preview, not a native layout.
The editor exposes text, X/Y and alignment only. Width, height, scale, weight,
color and visibility retain their defaults in the editor. The closed model
validates those properties too, including finite geometry, scale 0.4–2,
original text weight options and the ink/leaf/slate/loss color tokens.

## Component and scope contract

Import `WidgetsView` from `web/src/lab/widgets/WidgetsView.jsx` and supply a
nonempty string `scopeKey`. The host must bind that opaque key to both the
principal and project using its established authority; this component neither
derives nor verifies tenant identity. Missing, blank or non-string keys show
“Choose a project” and expose no draft controls. Keys are kept exactly as
provided, with no normalization or concatenation inside this component.

Mounted React state holds an independent draft, raw inputs and selection per
key. Switching keys restores that key's draft. `hidden` hides the view while
retaining state. Unmounting clears all drafts. IDs are stable for the element
lifetime and unique within the mounted editor. No storage, provider reads,
API, account lookup or session implementation is involved.

`widgetDoc.js` is the single validation boundary for editor and preview.
`WidgetPreview` takes validated text elements, revalidates the array boundary,
and presents React text nodes. Unknown fields (including actions, URLs, HTML,
code and bindings), other kinds, duplicate IDs and invalid values are refused.
Invalid edits stay in labelled controls with accessible errors; the entire
element's last valid preview stays in place until all its inputs are valid.
No input is silently truncated or a nonfinite number coerced into content.

## Replay and remaining proof

The parent verifier runs from the worktree root:

```sh
npm --prefix web run test:unit -- src/lab/widgets/widgets.test.jsx
```

The focused tests cover rendered add/edit/select/remove behavior and focus,
position and alignment, scope isolation and restoration, hide/show, unmount,
missing scope, literal HTML-looking text, invalid-input recovery, stable IDs,
the visible element limit and public model rejection boundaries.

For the later host/browser proof: mount with an explicit principal/project
key; add two elements, edit text and alignment, remove one; switch scopes and
return; hide/show; try `Infinity` for X and 513 text characters; confirm input
retention and the prior preview. Paste an HTML-looking string and confirm it
is literal. Inspect keyboard focus and all controls at narrow viewport widths.
The parent owner separately owns that browser proof, shared-shell integration,
audience decisions and any publication. Unit coverage alone does not complete
S02 product acceptance. Composition, two-host behavior, persistence and a
native renderer remain outside this slice.
