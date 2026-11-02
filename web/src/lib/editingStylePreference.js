export const EDITING_STYLE_PREFIX = 'leaf.editingStyle.v1:'
export function editingStyleKey(accountScope) {
  return typeof accountScope === 'string' && accountScope.startsWith('account:')
    ? `${EDITING_STYLE_PREFIX}${encodeURIComponent(accountScope)}` : null
}
export function readEditingStyle(accountScope, storage) {
  try {
    const key = editingStyleKey(accountScope)
    const target = storage === undefined ? globalThis.localStorage : storage
    return key && target?.getItem(key) === 'environment' ? 'environment' : 'sidebar'
  } catch { return 'sidebar' }
}
export function writeEditingStyle(accountScope, value, storage) {
  if (!['environment', 'sidebar'].includes(value)) return false
  try {
    const key = editingStyleKey(accountScope)
    const target = storage === undefined ? globalThis.localStorage : storage
    if (!key || !target) return false
    target.setItem(key, value)
    return true
  } catch { return false }
}
