import { STUDIO_PATHS } from '../../../scripts/next-runtime.mjs'

// Export only concrete known paths. nginx supplies the root shell for legacy
// production subpaths; next.config supplies that fallback explicitly in dev.
export const dynamicParams = false

export function generateStaticParams() {
  return STUDIO_PATHS.map((pathname) => ({ slug: pathname.split('/').filter(Boolean) }))
}

export default function StudioPage() {
  // The persistent layout owns the browser application, not this route leaf.
  return null
}
