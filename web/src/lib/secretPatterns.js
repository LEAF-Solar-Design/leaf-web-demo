// Credential shapes the command bar refuses to send (standardization slice 8a).
//
// THE GAP THIS CLOSES: before this module the composer forwarded an
// accidentally pasted API key straight into model context with zero friction.
//
// CONTRACT, stated here so every future edit conditions on it:
//   * PURE and React-free. No imports, no DOM, no I/O — the composer, a node
//     test and a jsdom component test all load it the same way.
//   * The credential VALUE never leaves this module. `findSecrets` returns
//     positions and identity only, NEVER the matched substring, so a hit can be
//     logged, telemetried and rendered without the secret ever being handled.
//     `maskForNotice` is the ONE function allowed to touch those characters,
//     and it emits at most MASK_PREFIX of them behind a fixed bullet run.
//   * LINEAR TIME on the hot path — this runs synchronously on every Enter.
//     EVERY quantifier here is explicitly bounded ({n,m}, never {n,}). An
//     unbounded greedy run followed by a required literal (the JWT dot is the
//     live example) is quadratic on adversarial input, so the segment caps are
//     load-bearing, not tidiness. Pinned by the 64 KB timing test.
//   * NO LOOKBEHIND. Left boundaries are matched as a capture group instead, so
//     the module runs on every browser the app ships to (a bare /sk-/ without a
//     left boundary fires on the word "task-", which is why the boundary is
//     there at all).
//   * FAILS CLOSED. Any hit refuses the dispatch. Only the deliberately fuzzy
//     generic pattern carries `overridable: true`.
//
// Regex objects are module-level and carry `g`, so `lastIndex` is reset before
// every scan (JS is single-threaded and findSecrets is synchronous, so no
// interleaving can observe a dirty index). No allocation per pattern per call.

/** Characters of the match a notice may show. A shape prefix, never entropy. */
export const MASK_PREFIX = 4
/** Fixed bullet count — the mask NEVER reveals the credential's length. */
export const MASK_BULLETS = 8
/** Bounds the result array so a pathological paste cannot allocate without limit. */
export const MAX_HITS = 16
/**
 * How near an AWS secret-shaped 40-char blob must sit to an access key id
 * before it counts. A bare 40-char token is indistinguishable from a hash, a
 * build id, or a run of drawing handles, so adjacency is what makes it a
 * finding rather than a false alarm on ordinary drawing data.
 */
export const AWS_PAIR_WINDOW = 512

// Left boundary for token shapes that begin with word characters. Group 1 is
// the boundary (or empty at input start); the credential is the NEXT group.
const B = '(^|[^A-Za-z0-9_-])'

/**
 * The frozen pattern table. `group` names the capture group holding the
 * credential itself; the `d` flag gives its exact offset via `match.indices`,
 * so the reported index is the credential's, never the boundary's.
 */
export const SECRET_PATTERNS = Object.freeze([
  Object.freeze({
    id: 'anthropic',
    label: 'Anthropic API key',
    overridable: false,
    group: 2,
    regex: new RegExp(`${B}(sk-ant-[A-Za-z0-9]{2,12}-[A-Za-z0-9_-]{16,512})`, 'gd'),
  }),
  Object.freeze({
    id: 'openai',
    label: 'OpenAI API key',
    overridable: false,
    group: 2,
    // `(?!ant-)` is a fixed-width guard at one position, not a branch inside a
    // repetition, so it costs O(1) per start and keeps the scan linear.
    regex: new RegExp(`${B}(sk-(?!ant-)[A-Za-z0-9_-]{20,512})`, 'gd'),
  }),
  Object.freeze({
    id: 'github',
    label: 'GitHub token',
    overridable: false,
    group: 2,
    regex: new RegExp(`${B}(gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})`, 'gd'),
  }),
  Object.freeze({
    id: 'aws_access_key',
    label: 'AWS access key ID',
    overridable: false,
    group: 2,
    regex: new RegExp(`${B}(AKIA[A-Z0-9]{16})(?![A-Z0-9])`, 'gd'),
  }),
  Object.freeze({
    id: 'aws_secret_key',
    label: 'AWS secret access key',
    overridable: false,
    group: 2,
    // Reported ONLY when an access key id sits within AWS_PAIR_WINDOW
    // characters — see `requiresNear`. Its own alphabet needs its own boundary
    // (base64 padding is not a word character).
    regex: /(^|[^A-Za-z0-9/+=])([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])/gd,
    requiresNear: 'aws_access_key',
  }),
  Object.freeze({
    id: 'slack',
    label: 'Slack token',
    overridable: false,
    group: 2,
    regex: new RegExp(`${B}(xox[abpr]-[A-Za-z0-9-]{10,255})`, 'gd'),
  }),
  Object.freeze({
    id: 'jwt',
    label: 'JSON Web Token',
    overridable: false,
    group: 2,
    // The {8,512} caps are the anti-quadratic guard: unbounded, a 64 KB run of
    // "eyJeyJeyJ…" makes every start position scan to end-of-input hunting the
    // dot. Real JWT segments are far inside 512.
    regex: new RegExp(`${B}(eyJ[A-Za-z0-9_-]{8,512}\\.eyJ[A-Za-z0-9_-]{8,512}\\.[A-Za-z0-9_-]{8,512})`, 'gd'),
  }),
  Object.freeze({
    id: 'private_key',
    label: 'private key',
    overridable: false,
    group: 1,
    // The armour header alone is the finding; the key body is never read.
    regex: /(-----BEGIN(?: [A-Z0-9]{1,20}){0,3} PRIVATE KEY-----)/gd,
  }),
  Object.freeze({
    id: 'generic',
    label: 'credential',
    // The ONE overridable shape: a labelled assignment is a strong hint, not
    // proof, so a tenant who means it can send anyway — once, by an explicit
    // click. Every named shape above is a hard refusal with no override.
    overridable: true,
    group: 1,
    regex: /(?:api[_-]?key|secret|token)[ \t]{0,8}[:=][ \t]{0,8}(\S{16,4096})/gid,
  }),
])

const AWS_ACCESS_KEY = SECRET_PATTERNS.find((p) => p.id === 'aws_access_key')

/** Exact start offset of a capture group, from the `d` flag's indices. */
function groupStart(match, group) {
  const at = match.indices && match.indices[group]
  return Array.isArray(at) ? at[0] : match.index
}

/** Every index at which an anchor pattern matches, for `requiresNear`. */
function anchorIndexes(text, pattern) {
  const out = []
  const re = pattern.regex
  re.lastIndex = 0
  let m
  while ((m = re.exec(text)) !== null) {
    out.push(groupStart(m, pattern.group))
    if (m[0].length === 0) re.lastIndex += 1 // zero-width guard: cannot spin
    if (out.length >= MAX_HITS) break
  }
  return out
}

function nearAny(index, anchors) {
  for (const a of anchors) if (Math.abs(a - index) <= AWS_PAIR_WINDOW) return true
  return false
}

/**
 * Scan `text` for credential shapes.
 *
 * Returns at most MAX_HITS findings, each `{id, label, overridable, index,
 * length}` — positions and identity ONLY. The matched characters are
 * deliberately absent so a caller can log, telemetry or render a finding
 * without ever holding the credential. Ordered by position.
 *
 * Non-string or empty input scans as empty.
 */
export function findSecrets(text) {
  if (typeof text !== 'string' || text.length === 0) return []
  const hits = []
  let awsAnchors = null
  for (const pattern of SECRET_PATTERNS) {
    const re = pattern.regex
    re.lastIndex = 0
    let m
    while ((m = re.exec(text)) !== null) {
      const captured = m[pattern.group]
      const empty = m[0].length === 0
      if (typeof captured !== 'string' || captured.length === 0) {
        if (empty) re.lastIndex += 1
        continue
      }
      const index = groupStart(m, pattern.group)
      if (pattern.requiresNear) {
        if (awsAnchors === null) awsAnchors = anchorIndexes(text, AWS_ACCESS_KEY)
        if (!nearAny(index, awsAnchors)) {
          if (empty) re.lastIndex += 1
          continue
        }
      }
      hits.push({
        id: pattern.id,
        label: pattern.label,
        overridable: pattern.overridable,
        index,
        length: captured.length,
      })
      if (empty) re.lastIndex += 1
      if (hits.length >= MAX_HITS) break
    }
    if (hits.length >= MAX_HITS) break
  }
  hits.sort((a, b) => a.index - b.index)
  return hits
}

/**
 * The ONLY function permitted to read a credential's characters: at most
 * MASK_PREFIX of them (a shape prefix such as "sk-a", never entropy) followed
 * by a FIXED bullet run, so neither the value nor its length is echoed.
 *
 * Any bad input degrades to bullets alone — it NEVER falls back to raw text.
 */
export function maskForNotice(text, hit) {
  const bullets = '•'.repeat(MASK_BULLETS)
  if (typeof text !== 'string' || !hit) return bullets
  const { index, length } = hit
  if (!Number.isInteger(index) || !Number.isInteger(length)) return bullets
  if (index < 0 || length <= 0 || index >= text.length) return bullets
  const take = Math.min(MASK_PREFIX, length)
  return `${text.slice(index, index + take)}${bullets}`
}
