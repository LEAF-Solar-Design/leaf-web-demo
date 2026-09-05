// W4f-8 / W4g-7: the prompt's EFFECTIVE inputs, one rule for every caller.
// The ribbon's live validation and the script runner both need the same
// answer to "what would this command run with": a point step's first field
// may hold the command line's point grammar ("x,y", "@dx,dy", "dist<angle",
// "@dist<angle") instead of a number, and it resolves against the step's
// anchor (the previous point: the chain point for a first point, the first
// point for a next point, the origin for a displacement) into both fields'
// effective values. A step whose decimal or edge operand is still empty is a
// step still waiting, not a mistake. Extracted from EngineRibbonClusters so
// the two consumers cannot drift (a hand-mirrored rule drifts).
//
// Pure and bounded: one pass over the prompt's steps, no allocation beyond
// the effective record and the failed set; every input is a string or absent.
import { isPointExpression, pointExpressionRefusal, resolvePointExpression } from './pointExpression.js'

/** A step is a point step when it asks for exactly two decimal operands. */
export function isPointStep(step) {
  return step.fields.length === 2 && step.fields.every(([, , mode = 'decimal']) => mode === 'decimal')
}

/**
 * `{ effective, expressionRefusal, failedExpression, waitingStep, pointSteps }`
 * for `prompt` over the operator record `inputs`, with `from` the chain point
 * a first point resolves against (or null).
 */
export function resolvePromptInputs(prompt, inputs, from = null) {
  const pointSteps = prompt ? prompt.steps.filter(isPointStep) : []
  const effective = { ...inputs }
  let expressionRefusal = ''
  // The fields whose expression did not resolve: outlined by name (a
  // malformed pair still parseFloats to its first number, so the numeric
  // test alone would not blame it).
  const failedExpression = new Set()
  let previousPoint = Array.isArray(from) && Number.isFinite(from[0]) && Number.isFinite(from[1]) ? [from[0], from[1]] : null
  for (const step of pointSteps) {
    const [[kx], [ky]] = step.fields
    const isDelta = kx === 'dx'
    const anchor = isDelta ? [0, 0] : previousPoint
    const raw = inputs[kx]
    if (isPointExpression(raw)) {
      const point = resolvePointExpression(raw, anchor)
      if (point) { effective[kx] = String(point[0]); effective[ky] = String(point[1]) } else {
        failedExpression.add(kx)
        if (!expressionRefusal) expressionRefusal = `${prompt.verb} refused: ${pointExpressionRefusal(raw, anchor)}`
      }
    }
    if (!isDelta) {
      const px = Number.parseFloat(effective[kx])
      const py = Number.parseFloat(effective[ky])
      if (Number.isFinite(px) && Number.isFinite(py)) previousPoint = [px, py]
    }
  }
  const waitingStep = prompt && !expressionRefusal
    ? prompt.steps.find((step) => step.fields.some(([key, , mode = 'decimal']) => (mode === 'decimal' || mode === 'edge') && String(effective[key] ?? '').trim() === '')) || null
    : null
  return { effective, expressionRefusal, failedExpression, waitingStep, pointSteps }
}
