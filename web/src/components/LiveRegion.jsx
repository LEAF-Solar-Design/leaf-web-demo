// ---------------------------------------------------------------------------
// LIVE REGION (standardization slice 6c). ONE primitive for every polite
// announcement in this client, replacing six hand-rolled regions that had
// drifted into three different shapes and two different ways of hiding
// themselves.
//
// THE ANNOUNCE CONTRACT, and the reason this is a component at all:
// a screen reader announces a live region's MUTATIONS. A region that arrives
// in the DOM already carrying its text has not mutated, so it is announced
// late, inconsistently, or not at all, depending on the reader. So the region
// element is rendered FIRST and its text is its CHILD, and every caller mounts
// the region permanently and lets the child go empty. DemoTour.jsx:243 already
// carried that lesson as a comment on its own hand-rolled region ("Kept
// permanently mounted so the live region pre-exists the unlock"); this file is
// where it now lives once.
//
// EXACT ATTRIBUTE PARITY is the hard contract of the slice, so the props below
// are the union of what the six sites actually had, not a tidied-up subset:
//
//   site                              role     atomic  hidden        label
//   App.jsx golden path               status   —       style         —
//   site/ToolCast.jsx run status      status   true    class         yes
//   components/ConversePanel.jsx log  log      false   —             yes
//   components/AnnotationDecisionCard —        —       —             —
//   cad/EditSurface.jsx               status   —       —             —
//   cadedit/CadEditSurface.jsx        status   —       —             —
//
// `role` is nullable and `atomic` is undefined-able ON PURPOSE: the decision
// card has neither attribute today, and `aria-atomic="false"` is NOT the same
// wire as no aria-atomic at all. Defaulting either one would silently rewrite
// a site's accessibility tree while the diff looked like a pure refactor.
// components/liveRegion.test.jsx is a table test over exactly those six rows.
//
// TWO WAYS TO HIDE, because the two shells genuinely have two: the stage's
// sheet carries an `.sr-only` utility and the console's does not (App.jsx's
// own comment: "styles inline because no .sr-only utility exists in the
// sheet"). Unifying them would be a stylesheet change wearing a refactor's
// clothes, so `visuallyHidden` names WHICH mechanism and the primitive owns
// both spellings.
//
// No state, no effects, no allocation on a render beyond the props object React
// builds anyway.
// ---------------------------------------------------------------------------
import { forwardRef } from "react";

//: The console's inline sr-only clip rect, moved from App.jsx verbatim.
//: Frozen so a consumer cannot mutate the shared object.
export const SR_ONLY_STYLE = Object.freeze({
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
});

//: `visuallyHidden` values. `false` is a VISIBLE region (the converse log and
//: the decision card are the region AND the visible text).
export const HIDE_WITH_CLASS = "class";
export const HIDE_WITH_STYLE = "style";

/**
 * LiveRegion.
 *
 *   as              the element to render ('div' | 'p'); the six sites use both
 *   role            'status' | 'log' | null. NULL renders no role attribute.
 *   live            the aria-live politeness. 'polite' everywhere today; the
 *                   prop exists so an assertive region is a value, not a
 *                   seventh hand-rolled node
 *   atomic          true | false | undefined -> aria-atomic="true" | "false" |
 *                   absent. Three states, all reachable, none defaulted
 *   visuallyHidden  false | 'class' (.sr-only) | 'style' (SR_ONLY_STYLE)
 *   label           aria-label, or null for no label attribute
 *   className       merged AFTER the hide class, so a caller's own class stays
 *   children        the announced text; empty is the normal resting state
 *
 * Any other prop (a ref, the converse log's onScroll) passes through to the
 * element, because the log region is also the scroll container.
 */
const LiveRegion = forwardRef(function LiveRegion(
  {
    as: Tag = "div",
    role,
    live = "polite",
    atomic,
    visuallyHidden,
    label,
    className,
    children,
    ...rest
  },
  ref,
) {
  const classes =
    visuallyHidden === HIDE_WITH_CLASS
      ? `sr-only${className ? ` ${className}` : ""}`
      : className;
  return (
    <Tag
      ref={ref}
      className={classes}
      aria-live={live}
      {...rest}
      role={role}
      aria-atomic={atomic}
      aria-label={label}
      style={visuallyHidden === HIDE_WITH_STYLE ? SR_ONLY_STYLE : undefined}
    >
      {children}
    </Tag>
  );
});

export default LiveRegion;
