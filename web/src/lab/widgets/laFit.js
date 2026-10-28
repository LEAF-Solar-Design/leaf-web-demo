// Renderer status line plus approval line inside the 160 pt Live Activity.
export const LA_USABLE_HEIGHT = 128

export function laFitProblem(doc) {
  if (!doc || typeof doc !== 'object' || !doc.formats || typeof doc.formats !== 'object' || Array.isArray(doc.formats)) return null
  const la = doc.formats.la
  if (!la || typeof la !== 'object' || Array.isArray(la)) return null
  const height = Number.isFinite(la.h) ? la.h : LA_USABLE_HEIGHT
  const overflow = Array.isArray(la.els) && la.els.some((element) =>
    element && Number.isFinite(element.y) && Number.isFinite(element.h) && element.y + element.h > height)
  if (height <= LA_USABLE_HEIGHT && !overflow) return null
  return 'The lock screen layout must fit in 128 pt. Lower its height or move elements up.'
}
