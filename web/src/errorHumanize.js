// Pure error humanizer — the last stop before an error string reaches the DOM.
//
// On stage a raw transport string ("POST /api/run -> 502") is worse than
// useless: it leaks the HTTP verb, the path and the status into the demo and
// reads as a crash. This module maps those to calm plain-English sentences.
//
// PURE by contract: no bundler-only globals, no React, no JSON imports — `node` can
// import it headless (web/scripts/check_errors.mjs does exactly that).

export const MSG_SERVICE = 'The service had a temporary problem — try again.'
export const MSG_NETWORK = "Couldn't reach the service — check the connection and retry."
export const MSG_GENERIC = 'Something went wrong — try again.'

// Anything that smells like transport plumbing: an HTTP verb, an /api/ path,
// a bare "-> 500" status arrow, an EventSource/job-stream complaint.
const VERB = /\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b/
const PATHY = /\/api\/|\bhttps?:\/\//i
const STATUS_ARROW = /->\s*\d{3}\b/
const JOBBY = /\block job\b|\blost job\b|\bjob [0-9a-z-]{6,}\b/i

const NETWORKY = /failed to fetch|networkerror|network error|err_(connection|network)|load failed|fetch failed|connection refused|econnrefused|timed? ?out|aborted/i

// A message we are willing to show verbatim: already a plain sentence, with no
// plumbing in it. Kept deliberately conservative.
function isPlainEnglish(s) {
  if (!s) return false
  if (VERB.test(s) || PATHY.test(s) || STATUS_ARROW.test(s) || JOBBY.test(s)) return false
  if (/[{}<>]|\bundefined\b|\bnull\b|\bNaN\b/.test(s)) return false
  if (/^[A-Za-z]*Error\b/.test(s)) return false
  if (s.length > 160) return false
  // Needs at least a couple of words to read as a sentence.
  return /[A-Za-z]/.test(s) && s.trim().split(/\s+/).length >= 2
}

/**
 * humanizeError(errOrString) -> a calm, leak-free sentence.
 * Never returns an HTTP verb, an /api/ path, or a "-> NNN" status.
 */
export function humanizeError(errOrString) {
  if (errOrString == null) return MSG_GENERIC

  const name = (errOrString && errOrString.name) || ''
  const raw = String(
    (errOrString && typeof errOrString === 'object' && (errOrString.message || errOrString.error)) ||
    errOrString ||
    '',
  ).trim()

  if (!raw) return MSG_GENERIC

  // Network / unreachable service first — a TypeError from fetch() lands here.
  if (NETWORKY.test(raw) || (name === 'TypeError' && /fetch/i.test(raw))) return MSG_NETWORK
  if (name === 'TypeError' && !isPlainEnglish(raw)) return MSG_NETWORK

  // Transport plumbing of any shape -> the calm service sentence.
  if (VERB.test(raw) || PATHY.test(raw) || STATUS_ARROW.test(raw) || JOBBY.test(raw)) return MSG_SERVICE

  if (isPlainEnglish(raw)) return raw
  return MSG_GENERIC
}

export default humanizeError
