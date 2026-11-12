import { sentence, withholds } from '../honesty.js'

export function formatCents(cents) {
  if (cents == null) return 'Not reported'
  if (typeof cents !== 'string' || cents.match(/^-?(0|[1-9][0-9]{0,12})(\.[0-9]{0,3}[1-9])?$/)?.[0] !== cents) throw Error('bad_cents')
  const negative = cents.startsWith('-') && cents !== '-0'
  const [whole, fraction = ''] = cents.replace(/^-/, '').split('.')
  const digits = whole.padStart(3, '0')
  return `${negative ? '-' : ''}$${digits.slice(0, -2)}.${digits.slice(-2)}${fraction}`
}

// Practice still projects numeric cents; Exchange ledger cents are strings.
export function practiceMoney(value) {
  if (value != null && typeof value !== 'number' && typeof value !== 'string') return 'Not reported'
  try { return formatCents(typeof value === 'number' ? String(value) : value) } catch { return 'Not reported' }
}
export const count = value => value == null ? 'Not reported' : String(value)
const decimal = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : 'Not reported'

export function blockLine(block) {
  if (block.reached == null) return `${sentence('trigger_not_measured')} Reached: not measured.`
  if (block.reached === 0) return 'Never reached in the window'
  return `Triggered ${count(block.triggered)} of ${block.reached} reached`
}

export function sideFigures(side = {}) {
  const measured = side.trades > 0
  return {
    trades: measured ? count(side.trades) : 'No trades',
    net: practiceMoney(side.net_after_fees_cents),
    netPerDollar: measured ? decimal(side.net_per_dollar_risked) : 'not measured',
    worst: measured ? practiceMoney(side.worst_trade_cents) : 'not measured',
    withoutBestWorst: side.trades >= 3 ? practiceMoney(side.without_best_and_worst_cents) : 'not measured',
    fees: practiceMoney(side.costs?.fees_cents),
    slippage: practiceMoney(side.costs?.slippage_cents),
  }
}

export function decisionTime(latency) {
  return latency?.p99_us > 0 ? `Decision time: ${decimal(latency.p99_us / 1000)} ms` : 'Decision time: not measured'
}

export function withheld(data) {
  return [data?.candidate, data?.live].some(side => side?.trades > 0 &&
    (side.costs?.fees_cents === 0 || side.costs?.fees_cents === '0' || side.costs?.fees_cents === '-0'))
    ? { code: 'zero_fee_with_trades', sentence: sentence('zero_fee_with_trades') } : null
}

export function scheduleLine(feeScheduleId) {
  return feeScheduleId ? `Practice fees: ${feeScheduleId}, whole cents per contract, rounded up (an upper bound). Exchange fee: taker 0.07 x C x P x (1 - P), rounded up once per order; maker 0.` : 'Fee schedule: not reported.'
}

export function registrationLines(rows) {
  if (rows === undefined) return ['Pre-registration: not reported. Clean days: not reported.']
  if (!rows.length) return ['No days reported yet.']
  return rows.flatMap(row => {
    const last = row.days_utc_last && new Date(`${row.days_utc_last}T00:00:00Z`)
    const end = last && Number.isFinite(last.getTime()) ? new Date(last.getTime() + 86400000).toISOString().slice(0, 10) : 'Not reported'
    const lines = [`Pre-registration: ${count(row.state)}.`,
      `Claimed UTC days ${row.days_utc_first ?? 'Not reported'} 00:00 to ${end} 00:00 (end exclusive)`,
      `Days reconciled green: ${row.days_reconciled_green ?? 'not measured'}`,
      `Clean days remaining: ${row.clean_days_remaining ?? 'not measured'}`]
    if (row.looks != null && row.lookers != null) lines.push(`Looked at ${row.looks} times by ${row.lookers} people.`)
    return lines
  })
}

export const OPERATION_LINE = 'Operation verified. No result verdict before the economics horizon.'
// Fails closed: a code the honesty table does not know withholds the day.
const withholdsDay = code => { try { return withholds(code) } catch { return true } }
const daySentence = code => { try { return sentence(code) } catch { return 'Result withheld.' } }
export function ledgerLines(rows) {
  if (rows === undefined) return { lines: ['Exchange ledger: not reporting. Tickets are not shown as P&L.'], withheldDays: [] }
  if (!rows.length) return { lines: ['No days reported yet.'], withheldDays: [] }
  const lines = [], withheldDays = []
  for (const row of rows) {
    const code = (Array.isArray(row.honesty_codes) ? row.honesty_codes : []).find(withholdsDay)
    if (row.withheld || code) {
      withheldDays.push({ day_utc: row.day_utc, sentence: code ? daySentence(code) : 'Result withheld.' })
    } else {
      const role = { owner: 'Your account', probe: 'Test order' }[row.role] ?? count(row.role)
      lines.push(`${count(row.day_utc)} · ${role} · Fills: ${count(row.fills)} · Exchange fee: ${formatCents(row.fee_cents)} · Exchange settled net: ${formatCents(row.settled_net_cents)} · ${count(row.label)}. Exchange basis, from the recorder. ${OPERATION_LINE}`)
    }
  }
  return { lines, withheldDays }
}

export const WINDOW_LINE = 'Last 4 hours (fixed). A 4-hour replay ranks nothing.'
