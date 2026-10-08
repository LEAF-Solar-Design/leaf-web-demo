export const SERVER_ERROR_CAP = 5
export const SERVER_DIAGNOSTIC_CHARS = 6000

export function extractErrorId(text) {
  if (typeof text !== 'string') return null
  return /error_id[=:]\s*([0-9a-f]{16})\b/.exec(text)?.[1] || null
}

export function serverDiagnostic(output, errorId, maxChars = SERVER_DIAGNOSTIC_CHARS) {
  if (typeof output !== 'string') return { matched: false, text: '' }
  const chars = typeof maxChars === 'number' ? maxChars : SERVER_DIAGNOSTIC_CHARS
  const index = typeof errorId === 'string' ? output.lastIndexOf(`[error_id=${errorId}]`) : -1
  if (index !== -1) {
    const lineStart = index === 0 ? 0 : output.lastIndexOf('\n', index - 1) + 1
    return { matched: true, text: output.slice(lineStart, lineStart + chars) }
  }
  return { matched: false, text: output.slice(-chars) }
}
