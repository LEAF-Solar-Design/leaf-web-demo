import { FontLoader } from 'three/examples/jsm/loaders/FontLoader.js'
import typeface from 'three/examples/fonts/helvetiker_regular.typeface.json' with { type: 'json' }

export const MAX_GLYPH_CHARS = 1024
let glyphFont

/** Bounded, unfilled glyph contours in the drawing's XY plane. */
export function textGlyphPolylines({ text, pt, height, rotationDeg = 0, handle = '', layer = '0', centered = false } = {}) {
  const finite = (v) => typeof v === 'number' && Number.isFinite(v)
  if (typeof text !== 'string' || !text.length || !Array.isArray(pt)
    || !finite(pt[0]) || !finite(pt[1]) || !finite(height) || height <= 0 || !finite(rotationDeg)) return []
  let count = 0
  let supported = ''
  for (const char of text) {
    if (++count > MAX_GLYPH_CHARS) return []
    supported += Object.prototype.hasOwnProperty.call(typeface.glyphs, char) ? char : '?'
  }
  glyphFont ??= new FontLoader().parse(typeface)
  const contours = []
  let minX = Infinity; let maxX = -Infinity
  for (const shape of glyphFont.generateShapes(supported, height / 1.013)) {
    for (const path of [shape, ...shape.holes]) {
      const samples = path.getPoints(4)
      if (!samples.length) continue
      const first = samples[0]
      const last = samples[samples.length - 1]
      if (first.x !== last.x || first.y !== last.y) samples.push(first.clone())
      for (const p of samples) {
        minX = Math.min(minX, p.x)
        maxX = Math.max(maxX, p.x)
      }
      contours.push(samples)
    }
  }
  const offset = centered && contours.length ? (minX + maxX) / 2 : 0
  const rad = rotationDeg * (Math.PI / 180)
  const cos = Math.cos(rad); const sin = Math.sin(rad)
  const z = finite(pt[2]) ? pt[2] : 0
  const polylines = contours.map((samples) => ({
    handle, layer, closed: false, strokeOnly: true,
    pts: samples.map((p) => {
      const x = p.x - offset
      return [pt[0] + x * cos - p.y * sin, pt[1] + x * sin + p.y * cos, z]
    }),
  }))
  return polylines.every((pl) => pl.pts.every((p) => p.every(finite))) ? polylines : []
}

// The server intake keys blocks by name; the engine projection lists records
// with a name field. One Viewer reads both shapes without changing the records.
// The server caps definitions at 200; 500 provides headroom, not a new policy.
export const MAX_BLOCK_DEFINITIONS = 500

function isPlainObject(value) {
  if (value === null || typeof value !== 'object') return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

export function blockDefinitions(intake) {
  const definitions = new Map()
  const blocks = intake?.blocks
  if (Array.isArray(blocks)) {
    for (let i = 0; i < Math.min(blocks.length, MAX_BLOCK_DEFINITIONS); i++) {
      const block = blocks[i]
      if (isPlainObject(block) && typeof block.name === 'string' && block.name.length > 0) {
        definitions.set(block.name.toUpperCase(), block)
      }
    }
  } else if (blocks !== null && typeof blocks === 'object') {
    let read = 0
    for (const key in blocks) {
      if (!Object.prototype.hasOwnProperty.call(blocks, key)) continue
      if (read >= MAX_BLOCK_DEFINITIONS) break
      read++
      const block = blocks[key]
      if (isPlainObject(block)) definitions.set(key.toUpperCase(), block)
    }
  }
  return definitions
}
