// Values are normalized board figures, never raw account or host records.
const DECIMAL = /^-?(?:0|[1-9]\d*)(?:\.\d{1,6})?$/
function fixed(value) {
  if (typeof value !== 'string' || !DECIMAL.test(value) || /^-0(?:\.0+)?$/.test(value)) throw new Error('Invalid figure.')
  const [whole, fraction = ''] = value.replace('-', '').split('.')
  const n = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, '0'))
  if (n > 1000000000000n) throw new Error('Figure outside bounds.')
  return value.startsWith('-') ? -n : n
}
function rounded(n, divisor) {
  return (n < 0n ? -1n : 1n) * ((n < 0n ? -n : n) + divisor / 2n) / divisor
}
export function formatMoney(value) {
  if (value == null) return 'Not reported'
  const cents = rounded(fixed(value), 1000000n)
  const abs = cents < 0n ? -cents : cents
  return `${cents < 0n ? '-' : ''}$${String(abs / 100n).replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${String(abs % 100n).padStart(2, '0')}`
}
export function formatPercent(value) {
  if (value == null) return 'Not reported'
  const n = rounded(fixed(value), 100n)
  const abs = n < 0n ? -n : n
  return `${n < 0n ? '-' : ''}${abs / 100n}${abs % 100n ? '.' + String(abs % 100n).padStart(2, '0').replace(/0+$/, '') : ''}%`
}
const money = new Set(['total', 'todayPnl', 'openPnlSum', 'cash', 'exposure', 'pnl', 'toWin', 'heroPnl', 'history', 'historyList'])
const percent = new Set(['winRate', 'odds', 'topOdds', 'pnlPct', 'todayPct'])
const columns = new Set(['symbols', 'pnl', 'pnlPct', 'odds', 'oddsBar', 'toWin', 'side', 'size', 'label', 'closes'])
export function resolveBinding(binding, board = {}, limit = 3) {
  if (!Number.isSafeInteger(limit) || limit < 1) throw new Error('Invalid row limit.')
  const field = binding.field
  let values
  if (columns.has(field)) {
    if (board.rows != null && !Array.isArray(board.rows)) throw new Error('Invalid board rows.')
    values = (board.rows ?? []).slice(0, Math.min(limit, 3)).map((row) => row[field] ?? null)
  } else if (['history', 'historyList', 'streak', 'spark'].includes(field)) {
    const data = board[field] ?? []
    if (!Array.isArray(data)) throw new Error('Invalid series.')
    values = data.slice(0, field === 'spark' ? 256 : field === 'streak' ? Math.min(limit, 10) : Math.min(limit, 4))
  } else {
    // A producer supplies the same explicit UTC window on every target.
    if (field === 'winRate' && board.winRate != null) {
      const w = board.window
      const validDay = (s) => typeof s === 'string' && /^20\d\d-\d\d-\d\dT00:00:00Z$/.test(s) && Number.isFinite(Date.parse(s)) && new Date(s).toISOString().replace('.000Z', 'Z') === s
      if (!w || !validDay(w.start) || !validDay(w.end) || Date.parse(w.end) - Date.parse(w.start) !== 86400000) throw new Error('Win rate requires an explicit UTC day.')
    }
    values = [board[field] ?? null]
  }
  return values.map((value) => {
    const numeric = value == null ? null : money.has(field) || percent.has(field) || ['spark', 'oddsBar'].includes(field) || binding.scale !== 'identity' ? Number(fixed(value)) / 1000000 : null
    const text = value == null ? 'Not reported' : money.has(field) ? formatMoney(value) : percent.has(field) ? formatPercent(value) : String(value)
    if (text.length > 512) throw new Error('Resolved text outside bounds.')
    let fraction = numeric
    if (binding.scale === 'today-percent') fraction = Math.max(0, Math.min(1, 0.5 + numeric * 10))
    if (binding.scale === 'open-count') fraction = Math.max(0, Math.min(1, numeric / 3))
    return { text, value: numeric, fraction: value == null ? null : fraction, tone: binding.tone === 'auto' ? numeric == null || numeric === 0 ? 'ink' : numeric < 0 ? 'loss' : 'leaf' : binding.tone }
  })
}
