// Renderer status line plus approval line inside the 160 pt Live Activity.
export const LA_USABLE_HEIGHT = 128

export function laFitProblem(doc) {
  const la = doc.formats.la
  if (!la || (la.h <= LA_USABLE_HEIGHT && la.els.every((element) => element.y + element.h <= la.h))) return null
  return 'The lock screen layout must fit in 128 pt. Lower its height or move elements up.'
}
