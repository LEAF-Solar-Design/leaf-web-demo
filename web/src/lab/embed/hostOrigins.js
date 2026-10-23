export const DEFAULT_HOST_ORIGINS = Object.freeze([
  'https://ops-dashboard-seven.vercel.app',
  'http://localhost:3000',
  'http://127.0.0.1:3000',
])

// An override replaces the list. Invalid entries never broaden host authority.
export function hostOrigins(value = import.meta.env?.VITE_LAB_HOST_ORIGINS) {
  const entries = value === undefined ? DEFAULT_HOST_ORIGINS : String(value).split(',')
  return [...new Set(entries.map((entry) => entry.trim()).filter((entry) => {
    try {
      const url = new URL(entry)
      return ['https:', 'http:'].includes(url.protocol) && url.origin === entry
        && !url.username && !url.password
    } catch { return false }
  }))]
}
