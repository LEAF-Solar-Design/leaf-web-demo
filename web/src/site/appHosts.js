// The app-only hosts: origins that serve the Studio and hand the sheets hub to
// the marketing site (contract/FRONT-DOOR.md). One list, read by the route
// redirect in SiteRoot.jsx and by the landing's sheet tabs, so the two can
// never disagree about where a sheet link leaves the site.
export const APP_ONLY_HOSTS = new Set([
  'leaf-platform-web.vercel.app',
  'app.leafdesign.ai',
  'platform.leafdesign.ai',
  'platform-staging.leafdesign.ai',
])

export const MARKETING_ORIGIN = 'https://www.leafautomation.ai'

/** True when this hostname redirects the sheets hub to the marketing site. */
export function isAppOnlyHost(hostname = typeof window === 'undefined' ? '' : window.location.hostname) {
  return APP_ONLY_HOSTS.has(String(hostname || '').toLowerCase())
}
