// One icon atom for every cockpit control (W4e slice G). The icons are
// icons8 "Fluency Systems Regular" SVGs, fetched by web/scripts/fetch_icons8.mjs
// into ONE external sprite (public/icons8-sprite.svg, a static asset outside
// the gzip bundle gate) and referenced by <use href="...#i8-<key>">. The
// sprite's inventory is assets/icons8/built.json, written by the same script,
// so a key the fetch has not resolved yet renders an honest two-letter glyph
// in the same box instead of an empty square. Fill is currentColor: the
// sprite strips every hard-coded fill so the chrome tints the icons.
import built from '../assets/icons8/built.json'
import { familyMonogram } from '../lib/surfaceRails.js'

const HAVE = new Set(Array.isArray(built.ids) ? built.ids : [])
export const SPRITE_URL = `/icons8-sprite.svg?v=${built.hash || '0'}`

export function hasIcon(id) {
  return !!id && HAVE.has(id)
}

export default function CockpitIcon({ id = '', fallback = '', size = 'small', decorative = true, label }) {
  if (import.meta.env?.DEV && !decorative && (typeof label !== 'string' || !label.trim())) {
    throw new Error('CockpitIcon requires a non-empty label when decorative is false')
  }
  const pixels = size === 'large' ? 39 : ['row', 'quick', 'strip'].includes(size) ? 16 : 18
  const accessibility = {
    'aria-hidden': decorative ? 'true' : undefined,
    role: decorative ? undefined : 'img',
    'aria-label': decorative ? undefined : label,
  }
  if (hasIcon(id)) {
    return (
      <svg className="ci" data-size={size} width={pixels} height={pixels} {...accessibility} focusable="false">
        <use href={`${SPRITE_URL}#i8-${id}`} />
      </svg>
    )
  }
  return <span className="ci ci-glyph" data-size={size} {...accessibility}>{familyMonogram(fallback || id)}</span>
}
