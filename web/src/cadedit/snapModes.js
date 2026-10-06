export const SNAP_MODES = Object.freeze([
  { kind: 'endpoint', bit: 1, label: 'Endpoint' },
  { kind: 'midpoint', bit: 2, label: 'Midpoint' },
  { kind: 'centre', bit: 4, label: 'Centre' },
  { kind: 'quadrant', bit: 16, label: 'Quadrant' },
  { kind: 'intersection', bit: 32, label: 'Intersection' },
  { kind: 'insertion', bit: 64, label: 'Insertion' },
  { kind: 'perpendicular', bit: 128, label: 'Perpendicular' },
  { kind: 'tangent', bit: 256, label: 'Tangent' },
  { kind: 'nearest', bit: 512, label: 'Nearest' },
].map(Object.freeze))
export const DEFAULT_SNAP_MODES = 23
export const ALL_SNAP_MODES = 1015
export function snapModeBit(kind) {
  return SNAP_MODES.find((mode) => mode.kind === kind)?.bit ?? 0
}
export function isSnapModeMask(value) {
  return Number.isSafeInteger(value) && value >= 0 && (value & ~1015) === 0
}
export function snapModeEnabled(mask, kind) {
  return isSnapModeMask(mask) && (mask & snapModeBit(kind)) !== 0
}
