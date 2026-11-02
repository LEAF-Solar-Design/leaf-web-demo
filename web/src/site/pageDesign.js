// Closed, optional ENV1 design data. Validation never inserts renderer defaults.
const choice = (...values) => value => values.includes(value)
const integer = (low, high) => value => Number.isInteger(value) && value >= low && value <= high
const color = value => typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value) && value.length === 7
const yes = value => value === true
const font = choice('classic', 'serif', 'sans', 'mono')
const align = choice('left', 'center', 'right')
const weight = choice('regular', 'medium', 'bold')
const aspect = choice('natural', 'square', 'landscape', 'portrait')
const focal = choice('center', 'top', 'bottom', 'left', 'right', 'top-left', 'top-right', 'bottom-left', 'bottom-right')
const fit = choice('contain', 'cover')
export const TEXT_DESIGN = {
  font, size: integer(12, 96), weight, color, align,
  lineHeight: value => typeof value === 'number' && Number.isFinite(value) && value >= 1 && value <= 2.5,
  letterSpacing: integer(-1, 8), italic: yes, underline: yes, hidden: yes,
}
export const CONTAINER_DESIGN = {
  align, columns: integer(1, 4), mobileColumns: integer(1, 2), gap: integer(0, 96),
  padding: integer(0, 96), maxWidth: integer(240, 1800), background: color,
  radius: integer(0, 64), borderWidth: integer(0, 8), borderColor: color,
  shadow: choice('none', 'soft', 'strong'), hidden: yes,
}
export const PHOTO_DESIGN = { fit, aspect, focal, span: integer(1, 2), radius: integer(0, 64), hidden: yes }
export function validateDesign(value, rules) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !Object.keys(value).length
    || Object.entries(value).some(([key, item]) => !Object.hasOwn(rules, key) || !rules[key](item))) {
    throw new Error('Unsupported page design. Reset the invalid property.')
  }
  return true
}
const object = rules => value => validateDesign(value, rules)
const text = high => value => typeof value === 'string' && value.trim().length > 0 && Array.from(value).length <= high
  && !/[<>]/.test(value) && !/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value.replaceAll('\n', ''))
  && !/(?:[a-z][a-z0-9+.-]*:\/\/|\b(?:https?|ftp|file|data|javascript):|\bwww\.)/i.test(value)
const palette = { background: color, text: color, muted: color, accent: color }
export const PAGE_APPEARANCE = {
  font, scale: choice('compact', 'regular', 'large'), spacing: choice('tight', 'regular', 'airy'),
  width: choice('narrow', 'regular', 'wide'), palette: object(palette),
  gap: CONTAINER_DESIGN.gap, padding: CONTAINER_DESIGN.padding, maxWidth: CONTAINER_DESIGN.maxWidth,
}
export const PORTFOLIO_APPEARANCE = {
  ...PAGE_APPEARANCE, headingWeight: weight, align, columns: CONTAINER_DESIGN.columns,
  mobileColumns: CONTAINER_DESIGN.mobileColumns, imageFit: fit, aspect, focal,
}
const navigationText = Object.fromEntries(Object.entries(TEXT_DESIGN).filter(([key]) => key !== 'hidden'))
export const NAV_DESIGN = {
  homeLabel: text(40), aboutLabel: text(40), resumeLabel: text(40),
  order: value => Array.isArray(value) && new Set(value).size === value.length
    && value.every(item => ['home', 'galleries', 'about', 'resume'].includes(item)),
  hideHome: yes, hideAbout: yes, hideResume: yes, hidden: yes, layout: choice('row', 'column'),
  appearance: object(navigationText), gap: CONTAINER_DESIGN.gap, padding: CONTAINER_DESIGN.padding,
  background: color, radius: CONTAINER_DESIGN.radius,
}
const styles = names => object(Object.fromEntries(names.split(' ').map(name => [name, object(TEXT_DESIGN)])))
export const DESIGN_FIELDS = {
  page: { appearance: object(PAGE_APPEARANCE), textStyles: styles('label headline description status') },
  portfolio: {
    appearance: object(PORTFOLIO_APPEARANCE), textStyles: styles('name headline introduction about aboutHeading contactHeading location email resumeHeading'),
    nav: object(NAV_DESIGN), labels: object(Object.fromEntries(['aboutHeading', 'contactHeading', 'resumeHeading', 'viewerClose'].map(key => [key, text(80)]))),
  },
  category: { layout: object(CONTAINER_DESIGN), hidden: yes, navLabel: text(40), textStyles: styles('title description navigation') },
  image: { display: object(PHOTO_DESIGN), captionStyle: object(TEXT_DESIGN) },
  section: { layout: object(CONTAINER_DESIGN), headingStyle: object(TEXT_DESIGN) },
  entry: { layout: object(CONTAINER_DESIGN), textStyles: styles('title subtitle dates description') },
}
export function validateNodeDesign(node, kind) {
  for (const [key, rule] of Object.entries(DESIGN_FIELDS[kind])) {
    if (Object.hasOwn(node, key) && !rule(node[key])) throw new Error(`Unsupported ${key}.`)
  }
}

export const fonts = { classic: 'Georgia, serif', serif: 'Georgia, serif', sans: 'Arial, sans-serif', mono: 'monospace' }
export function textStyle(value = {}) {
  return {
    fontFamily: fonts[value.font], fontSize: value.size, fontWeight: { regular: 400, medium: 500, bold: 700 }[value.weight],
    color: value.color, textAlign: value.align, lineHeight: value.lineHeight, letterSpacing: value.letterSpacing,
    fontStyle: value.italic ? 'italic' : undefined, textDecoration: value.underline ? 'underline' : undefined,
    display: value.hidden ? 'none' : value.align || value.size || value.lineHeight ? 'block' : undefined,
  }
}
export function containerStyle(value = {}) {
  return { textAlign: value.align, '--design-columns': value.columns, '--design-mobile-columns': value.mobileColumns,
    '--design-gap': value.gap === undefined ? undefined : `${value.gap}px`, gap: value.gap, padding: value.padding, maxWidth: value.maxWidth === undefined ? undefined : `min(100%, ${value.maxWidth}px)`,
    background: value.background, borderRadius: value.radius, borderWidth: value.borderWidth,
    borderStyle: value.borderWidth === undefined ? undefined : 'solid', borderColor: value.borderColor,
    boxShadow: { none: 'none', soft: '0 2px 12px #0002', strong: '0 8px 28px #0005' }[value.shadow],
    display: value.hidden ? 'none' : undefined,
  }
}
export function photoStyle(display = {}, appearance = {}) {
  const aspectValue = display.aspect ?? appearance.aspect
  return { objectFit: display.fit ?? appearance.imageFit, objectPosition: (display.focal ?? appearance.focal)?.replace('-', ' '),
    aspectRatio: { natural: 'auto', square: '1', landscape: '4 / 3', portrait: '3 / 4' }[aspectValue],
    height: aspectValue ? 'auto' : undefined,
    borderRadius: display.radius, display: display.hidden ? 'none' : undefined,
  }
}
export function appearanceStyle(appearance = {}) {
  const palette = appearance.palette || {}
  return { ...containerStyle(appearance), fontFamily: appearance.font === 'classic' ? 'Arial, sans-serif' : fonts[appearance.font],
    '--design-scale': { compact: .85, regular: 1, large: 1.2 }[appearance.scale],
    '--design-spacing': { tight: '16px', regular: '32px', airy: '48px' }[appearance.spacing],
    '--design-weight': { regular: 400, medium: 500, bold: 700 }[appearance.headingWeight],
    '--design-width': { narrow: '720px', regular: '1200px', wide: '1600px' }[appearance.width],
    '--design-font': fonts[appearance.font],
    '--page-bg': palette.background, '--page-fg': palette.text, '--page-muted': palette.muted,
    '--design-accent': palette.accent, background: palette.background, color: palette.text,
  }
}
export function photoSpanStyle(display = {}, document, category) {
  if (display.span === undefined) return {}
  const columns = category?.layout?.columns ?? document.appearance?.columns ?? (document.galleryLayout === 'single' ? 1 : 2)
  const mobile = category?.layout?.mobileColumns ?? document.appearance?.mobileColumns ?? 1
  return { gridColumn: `span ${Math.min(display.span, columns)}`, '--design-mobile-span': Math.min(display.span, mobile) }
}
export function navigationOrder(nav = {}) {
  return [...(nav.order || []), ...['home', 'galleries', 'about', 'resume'].filter(key => !nav.order?.includes(key))]
}
