import { listProjectPhotos } from '../projects/api.js'

export const PORTFOLIO_SCHEMA = 'leaf.portfolio.v1'
const HEX = /^[0-9a-f]{64}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const EMAIL = /^[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*@[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)+$/
const clone = value => JSON.parse(JSON.stringify(value))
const fullMatch = (pattern, value) => typeof value === 'string' && pattern.exec(value)?.[0] === value
function exact(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(' ') !== keys.split(' ').sort().join(' ')) throw new Error('Unsupported portfolio fields.')
}
function text(value, low, high, label) {
  if (typeof value !== 'string' || Array.from(value).length < low || Array.from(value).length > high || (low && !value.trim())
    || /[<>]/.test(value) || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(value.replaceAll('\n', ''))
    || /(?:[a-z][a-z0-9+.-]*:\/\/|\b(?:https?|ftp|file|data|javascript):|\bwww\.)/i.test(value)) {
    throw new Error(`Check ${label}: use plain text, ${low}–${high} characters.`)
  }
}
function items(value, low, high) {
  if (!Array.isArray(value) || value.length < low || value.length > high) throw new Error(`Use ${low}–${high} items.`)
}

export function validatePortfolioDocument(document, importing = false) {
  exact(document, 'schema name headline introduction about contact resume categories theme')
  if (document.schema !== (importing ? 'leaf.portfolio.import.v1' : PORTFOLIO_SCHEMA) || document.theme !== 'light') throw new Error('Unsupported portfolio schema or theme.')
  for (const [field, low, high] of [['name', 1, 120], ['headline', 1, 240], ['introduction', 0, 2400], ['about', 0, 8000]]) text(document[field], low, high, field)
  exact(document.contact, 'email location')
  const email = document.contact.email
  if (typeof email !== 'string' || email.length > 254 || (email && !fullMatch(EMAIL, email))) throw new Error('Use a valid email or leave it empty.')
  text(document.contact.location, 0, 160, 'location')
  items(document.resume, 0, 8)
  for (const section of document.resume) {
    exact(section, 'heading entries'); text(section.heading, 1, 120, 'section heading'); items(section.entries, 0, 20)
    for (const entry of section.entries) {
      exact(entry, 'title subtitle dates description')
      for (const [field, high] of [['title', 200], ['subtitle', 240], ['dates', 120], ['description', 2400]]) text(entry[field], 0, high, field)
    }
  }
  items(document.categories, 1, 12)
  const slugs = new Set(), references = new Map()
  let total = 0
  for (const category of document.categories) {
    exact(category, 'slug title description hero images')
    if (!fullMatch(/^[a-z][a-z0-9-]{0,47}$/, category.slug) || ['home', 'about', 'resume'].includes(category.slug) || slugs.has(category.slug)) throw new Error('Choose unique category slugs; home, about and resume are reserved.')
    slugs.add(category.slug)
    text(category.title, 1, 120, 'category title'); text(category.description, 0, 1200, 'category description')
    items(category.images, 1, 200)
    total += category.images.length
    if (total > 200 || !Number.isInteger(category.hero) || category.hero < 0 || category.hero >= category.images.length) throw new Error('Choose a hero image and use at most 200 images in total.')
    const seen = new Set()
    for (const image of category.images) {
      exact(image, importing ? 'source_sha256 title alt' : 'material_id sha256 preview_sha256 title alt')
      if (importing) {
        if (!fullMatch(HEX, image.source_sha256)) throw new Error('Invalid source photo digest.')
      } else {
        if (!fullMatch(UUID, image.material_id) || seen.has(image.material_id)) throw new Error('Invalid or duplicate photo reference in category.')
        seen.add(image.material_id)
        for (const field of ['sha256', 'preview_sha256']) if (!fullMatch(HEX, image[field])) throw new Error('Invalid photo digest.')
        const pair = `${image.sha256}:${image.preview_sha256}`
        if (references.has(image.material_id) && references.get(image.material_id) !== pair) throw new Error('Conflicting photo digests.')
        references.set(image.material_id, pair)
      }
      text(image.title, 0, 200, 'image caption'); text(image.alt, 1, 400, 'image alt text')
    }
  }
  if (new TextEncoder().encode(JSON.stringify(document)).length > 512 * 1024) throw new Error('Portfolio exceeds 512 KiB.')
  return clone(document)
}

export async function projectPortfolioPhotos(projectId, options) {
  const photos = [], cursors = new Set()
  let cursor
  do {
    const result = await listProjectPhotos(projectId, { ...options, cursor })
    if (options?.signal?.aborted) throw new DOMException('Cancelled', 'AbortError')
    if (!Array.isArray(result.materials)) throw new Error('Project photos could not be read. Reload photos.')
    photos.push(...result.materials)
    if (photos.length > 200) throw new Error('This project has more than 200 photos. Use a project with at most 200 photos for portfolio selection.')
    cursor = result.next_cursor
    if (cursor && cursors.has(cursor)) throw new Error('Photo pagination failed. Reload photos.')
    if (cursor) cursors.add(cursor)
  } while (cursor)
  return photos
}

export const photoReference = photo => ({ material_id: photo.material_id, sha256: photo.sha256, preview_sha256: photo.preview_sha256, title: '', alt: photo.name || 'Photograph' })
export function emptyPortfolio() {
  return { schema: PORTFOLIO_SCHEMA, name: 'Your portfolio', headline: 'Selected work', introduction: '', about: '', contact: { email: '', location: '' }, resume: [], categories: [{ slug: 'selected-work', title: 'Selected work', description: '', hero: 0, images: [] }], theme: 'light' }
}
export async function importPortfolio(file, projectId, options) {
  if (!file || file.size > 2 * 1024 * 1024 || !/\.json$/i.test(file.name)) throw new Error('Choose a JSON portfolio file of at most 2 MiB.')
  let document
  try { document = JSON.parse(await file.text()) } catch { throw new Error('The selected file is not valid JSON.') }
  document = validatePortfolioDocument(document, true)
  const photos = await projectPortfolioPhotos(projectId, options)
  const problems = new Set()
  for (const category of document.categories) category.images = category.images.map(image => {
    const matches = photos.filter(photo => photo.sha256 === image.source_sha256)
    if (matches.length !== 1) { problems.add(`${matches.length ? 'Ambiguous' : 'Missing'} photo ${image.source_sha256}. ${matches.length ? 'Keep exactly one matching upload.' : 'Upload the original photo to this project, then retry.'}`); return image }
    return { ...photoReference(matches[0]), title: image.title, alt: image.alt }
  })
  if (problems.size) throw new Error([...problems].join('\n'))
  document.schema = PORTFOLIO_SCHEMA
  return validatePortfolioDocument(document)
}
