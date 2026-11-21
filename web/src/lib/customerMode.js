// Chrome preference only; authorization continues to use the server entitlements.
export function isCustomerMode(session = {}) {
  const values = typeof session === 'string' ? [session] : [session?.plan, session?.tier, session?.role]
  if (values.some((value) => /^(admin|dev)$/i.test(value || ''))) return false
  if (typeof window === 'undefined') return true
  if (new URLSearchParams(window.location.search).get('dev') === '1') return false
  try {
    if (window.localStorage.getItem('leaf.devChrome') === '1') return false
  } catch { /* Storage may be unavailable in private or embedded contexts. */ }
  return true
}
