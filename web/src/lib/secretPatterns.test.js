/**
 * The credential-shape table (standardization slice 8a).
 *
 * Three invariants this file exists to hold, in priority order:
 *   1. NO VALUE LEAK — the result of findSecrets must never contain the input's
 *      credential characters, because callers log and telemetry it.
 *   2. NO FALSE REFUSAL on the drawing vocabulary the command bar actually
 *      carries: prose, hex ids, drawing handles, tool names, layer names.
 *   3. LINEAR TIME — 64 KB of adversarial near-misses per pattern, under the
 *      50 ms budget, because the scan runs synchronously on every Enter.
 */
import { describe, expect, it } from 'vitest'
import {
  AWS_PAIR_WINDOW,
  MASK_BULLETS,
  MASK_PREFIX,
  MAX_HITS,
  SECRET_PATTERNS,
  findSecrets,
  maskForNotice,
  evaluateSecretGuard,
} from './secretPatterns.js'

// Every credential below is FAKE and structurally valid only — never a real key.
const AWS_ID = 'AKIAIOSFODNN7EXAMPLE'
const AWS_SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'

/** id -> [positive samples], [negatives that MUST NOT match this id] */
const TABLE = [
  {
    id: 'anthropic',
    label: 'Anthropic API key',
    overridable: false,
    positive: [
      `sk-ant-api03-${'A9_-'.repeat(12)}`,
      `sk-ant-oat01-${'Zx1'.repeat(10)}`,
      `paste: sk-ant-api03-${'q'.repeat(30)} thanks`,
    ],
    negative: ['sk-ant-', 'sk-ant-api03-short', 'ask-ant-api03-aaaaaaaaaaaaaaaaaaaa'],
  },
  {
    id: 'openai',
    label: 'OpenAI API key',
    overridable: false,
    positive: [`sk-${'proj1A'.repeat(6)}`, `key is sk-${'b'.repeat(48)}.`],
    negative: [
      'sk-short',
      // The reason a left boundary exists at all: "task-" contains "sk-".
      'move the task-list panel and the desk-lamp block to layer 2',
      `sk-ant-api03-${'A'.repeat(30)}`, // owned by `anthropic`, never double-reported here
    ],
  },
  {
    id: 'github',
    label: 'GitHub token',
    overridable: false,
    positive: [
      `ghp_${'a1B2'.repeat(8)}`,
      `gho_${'c'.repeat(36)}`,
      `ghs_${'d'.repeat(36)}`,
      `ghu_${'e'.repeat(36)}`,
      `github_pat_${'11ABCDEFG_'.repeat(4)}`,
    ],
    negative: ['ghp_short', 'github_pattern is a normal English phrase', 'ghx_aaaaaaaaaaaaaaaaaaaaaaaa'],
  },
  {
    id: 'aws_access_key',
    label: 'AWS access key ID',
    overridable: false,
    positive: [AWS_ID, `id=${AWS_ID} region=us-west-2`],
    negative: ['AKIA', 'AKIATOOSHORT', 'SAKIAIOSFODNN7EXAMPLE'],
  },
  {
    id: 'aws_secret_key',
    label: 'AWS secret access key',
    overridable: false,
    // Only adjacent to an access key id — a bare 40-char blob is a hash.
    positive: [`${AWS_ID} ${AWS_SECRET}`, `${AWS_SECRET}\nid ${AWS_ID}`],
    negative: [
      AWS_SECRET, // alone: indistinguishable from a digest
      '0123456789abcdef0123456789abcdef01234567',
      // 64-char sha256 contains 40-char windows; none may be reported.
      'a'.repeat(64),
    ],
  },
  {
    id: 'slack',
    label: 'Slack token',
    overridable: false,
    positive: ['xoxb-123456789012-abcdefghijkl', 'xoxp-1111111111-2222222222-abcdef', 'xoxa-2-abcdefghijkl', 'xoxr-abcdefghijklmno'],
    negative: ['xoxb-', 'xoxz-123456789012-abcdefghijkl', 'boxoxb-123456789012-abcdefghijkl'],
  },
  {
    id: 'jwt',
    label: 'JSON Web Token',
    overridable: false,
    positive: [
      'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk',
    ],
    negative: [
      'eyJhbGciOiJIUzI1NiJ9', // header alone
      'eyJhbGciOiJIUzI1NiJ9.eyJzdWIi', // two segments, no signature
      'monkeyJumps.eyJabcdefgh.short',
    ],
  },
  {
    id: 'private_key',
    label: 'private key',
    overridable: false,
    positive: [
      '-----BEGIN PRIVATE KEY-----',
      '-----BEGIN RSA PRIVATE KEY-----\nMIIE...',
      '-----BEGIN OPENSSH PRIVATE KEY-----',
      '-----BEGIN ENCRYPTED PRIVATE KEY-----',
    ],
    negative: ['-----BEGIN CERTIFICATE-----', '-----BEGIN PUBLIC KEY-----', 'begin private key'],
  },
  {
    id: 'generic',
    label: 'credential',
    overridable: true,
    positive: [
      `api_key: ${'x'.repeat(20)}`,
      `api-key=${'y'.repeat(16)}`,
      `apikey = ${'z'.repeat(24)}`,
      `SECRET=${'q'.repeat(18)}`,
      `token: ${'w'.repeat(32)}`,
    ],
    negative: [
      'api_key: short',
      'the secret sauce is in the layer naming convention',
      'keep this a secret between us and the drafting team',
      'token economics are not relevant to this drawing',
      'api key rotation is scheduled for next quarter',
    ],
  },
]

// Ordinary command-bar traffic. NOTHING here may be refused: a false refusal on
// the daily vocabulary is how a guard gets ripped out.
const ORDINARY = [
  'Rearrange the existing panels in this drawing into the shape of a sitting cat.',
  'Count entities by layer and show me the totals',
  '/count-by-layer',
  'delete the entity with handle 2F1A',
  'handles 2F1A 3B4C 1A0 and 4C2D are all on layer PV-ARRAY',
  'the drawing sha is 0123456789abcdef0123456789abcdef',
  'commit 60d8a051 fixed the version lock',
  'move the task-list to the desk-side rail',
  'set the string sizer token count to 12', // "token" with no assignment
  'the secret is that this is just prose',
  'AKIA is an AWS prefix, discussed here without a key',
  'run 0123456789abcdef0123456789abcdef01234567 again',
]

const idsOf = (text) => findSecrets(text).map((h) => h.id)

describe('the pattern table itself', () => {
  it('is frozen, entry by entry', () => {
    expect(Object.isFrozen(SECRET_PATTERNS)).toBe(true)
    for (const p of SECRET_PATTERNS) expect(Object.isFrozen(p)).toBe(true)
  })

  it('has unique ids and exactly one overridable shape', () => {
    const ids = SECRET_PATTERNS.map((p) => p.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(SECRET_PATTERNS.filter((p) => p.overridable).map((p) => p.id)).toEqual(['generic'])
  })

  it('covers exactly the ids this table tests', () => {
    expect(SECRET_PATTERNS.map((p) => p.id)).toEqual(TABLE.map((t) => t.id))
  })

  it('bounds every quantifier — an open-ended {n,} is the quadratic trap', () => {
    for (const p of SECRET_PATTERNS) {
      expect(p.regex.source, `${p.id} has an unbounded quantifier`).not.toMatch(/\{\d+,\}/)
    }
  })

  it('uses no lookbehind, which is not safe on every shipped browser', () => {
    for (const p of SECRET_PATTERNS) {
      expect(p.regex.source, `${p.id} uses lookbehind`).not.toContain('(?<')
    }
  })
})

describe.each(TABLE)('$id', ({ id, label, overridable, positive, negative }) => {
  it.each(positive)('refuses %s', (sample) => {
    const hits = findSecrets(sample)
    const hit = hits.find((h) => h.id === id)
    expect(hit, `expected a ${id} hit`).toBeTruthy()
    expect(hit.label).toBe(label)
    expect(hit.overridable).toBe(overridable)
    expect(hit.index).toBeGreaterThanOrEqual(0)
    expect(hit.length).toBeGreaterThan(0)
    // The reported slice really is the credential, without the result ever
    // carrying it: we re-derive it from the ORIGINAL text using index+length.
    expect(sample.slice(hit.index, hit.index + hit.length).length).toBe(hit.length)
  })

  it.each(negative)('lets %s through', (sample) => {
    expect(idsOf(sample)).not.toContain(id)
  })
})

describe('the result never carries the value', () => {
  it.each(TABLE.flatMap((t) => t.positive.map((s) => [t.id, s])))(
    '%s: the serialized result contains no run of the input',
    (_id, sample) => {
      const hits = findSecrets(sample)
      expect(hits.length).toBeGreaterThan(0)
      const serialized = JSON.stringify(hits)
      for (const hit of hits) {
        const value = sample.slice(hit.index, hit.index + hit.length)
        expect(serialized).not.toContain(value)
        // Not even a fragment. Every 8-character window of the credential must
        // be absent, EXCEPT where it is a substring of the finding's own id or
        // label — "github_pat_…" legitimately shares letters with "GitHub
        // token", and that shared prefix is public shape, not entropy.
        const vocabulary = `${hit.id} ${hit.label}`.toLowerCase()
        for (let i = 0; i + 8 <= value.length; i += 1) {
          const window = value.slice(i, i + 8)
          if (vocabulary.includes(window.toLowerCase())) continue
          expect(serialized, `leaked window "${window}"`).not.toContain(window)
        }
      }
      // The finding's own key set is closed — a future field cannot smuggle it.
      for (const hit of hits) {
        expect(Object.keys(hit).sort()).toEqual(['id', 'index', 'label', 'length', 'overridable'])
      }
    },
  )
})

describe('ordinary command-bar text', () => {
  it.each(ORDINARY)('is never refused: %s', (sample) => {
    expect(findSecrets(sample)).toEqual([])
  })
})

describe('findSecrets bounds and shape', () => {
  it('returns [] for non-strings and empty input', () => {
    for (const bad of [null, undefined, 0, 42, {}, [], () => {}, '']) {
      expect(findSecrets(bad)).toEqual([])
    }
  })

  it('caps the finding count so a pathological paste cannot allocate freely', () => {
    const many = `${`ghp_${'a'.repeat(30)} `.repeat(200)}`
    expect(findSecrets(many).length).toBeLessThanOrEqual(MAX_HITS)
  })

  it('orders findings by position', () => {
    const text = `first ${AWS_ID} then ghp_${'b'.repeat(30)} then sk-ant-api03-${'c'.repeat(20)}`
    const hits = findSecrets(text)
    expect(hits.length).toBeGreaterThan(1)
    for (let i = 1; i < hits.length; i += 1) {
      expect(hits[i].index).toBeGreaterThanOrEqual(hits[i - 1].index)
    }
  })

  it('is idempotent — a stale regex lastIndex would break the second scan', () => {
    const text = `sk-ant-api03-${'d'.repeat(30)}`
    expect(findSecrets(text)).toEqual(findSecrets(text))
    expect(findSecrets(text)).toEqual(findSecrets(text))
  })

  it('drops an AWS secret that drifts beyond the pairing window', () => {
    const far = `${AWS_ID}${' '.repeat(AWS_PAIR_WINDOW + 40)}${AWS_SECRET}`
    expect(idsOf(far)).toEqual(['aws_access_key'])
  })
})

describe('maskForNotice', () => {
  it('shows at most the shape prefix, then a fixed bullet run', () => {
    const sample = `sk-ant-api03-${'e'.repeat(30)}`
    const [hit] = findSecrets(sample)
    const masked = maskForNotice(sample, hit)
    expect(masked).toBe(`sk-a${'•'.repeat(MASK_BULLETS)}`)
    expect(masked.length).toBe(MASK_PREFIX + MASK_BULLETS)
  })

  it('shows bullets ALONE for a shape whose match is the value (generic, aws secret)', () => {
    // For these two the capture group IS the credential, so its first four
    // characters are entropy, not shape; a prefix here would be a leak.
    const bullets = '•'.repeat(MASK_BULLETS)
    const generic = 'api_key=supersecretvalue1234567890'
    const [g] = findSecrets(generic)
    expect(g.id).toBe('generic')
    expect(maskForNotice(generic, g)).toBe(bullets)
    const secret = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
    const pair = `${secret} AKIAIOSFODNN7EXAMPLE`
    const hits = findSecrets(pair)
    const aws = hits.find((h) => h.id === 'aws_secret_key')
    expect(aws).toBeTruthy()
    expect(maskForNotice(pair, aws)).toBe(bullets)
    expect(maskForNotice(pair, aws)).not.toContain(secret.slice(0, 4))
    // and the whole refusal path agrees, not only the helper
    expect(evaluateSecretGuard(generic).masked).toBe(bullets)
  })

  it('every pattern declares shapePrefix, and a true one really opens with fixed characters', () => {
    for (const p of SECRET_PATTERNS) {
      expect(typeof p.shapePrefix, p.id).toBe('boolean')
    }
    const fixed = { anthropic: 'sk-a', openai: 'sk-', github: 'gh', aws_access_key: 'AKIA', slack: 'xox', jwt: 'eyJ', private_key: '----' }
    for (const [id, head] of Object.entries(fixed)) {
      const p = SECRET_PATTERNS.find((x) => x.id === id)
      expect(p.shapePrefix, id).toBe(true)
      expect(p.regex.source.replace(/^\(\^\|\[\^[^\]]*\]\)\(?/, '').replace(/^\(/, '').replace(/^-----/, '----')).toMatch(new RegExp('^' + head.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    }
    for (const id of ['aws_secret_key', 'generic']) {
      expect(SECRET_PATTERNS.find((x) => x.id === id).shapePrefix, id).toBe(false)
    }
  })
  it('never reveals the credential length', () => {
    const short = `ghp_${'f'.repeat(20)}`
    const long = `ghp_${'f'.repeat(200)}`
    expect(maskForNotice(short, findSecrets(short)[0]))
      .toBe(maskForNotice(long, findSecrets(long)[0]))
  })

  it('degrades to bullets alone rather than echoing raw text', () => {
    const bullets = '•'.repeat(MASK_BULLETS)
    expect(maskForNotice('anything', null)).toBe(bullets)
    expect(maskForNotice(null, { index: 0, length: 4 })).toBe(bullets)
    expect(maskForNotice('abc', { index: -1, length: 4 })).toBe(bullets)
    expect(maskForNotice('abc', { index: 99, length: 4 })).toBe(bullets)
    expect(maskForNotice('abc', { index: 0, length: 0 })).toBe(bullets)
    expect(maskForNotice('abc', { index: 1.5, length: 4 })).toBe(bullets)
  })
})

describe('linear time on 64 KB of adversarial near-misses', () => {
  const KB64 = 65536
  const BUDGET_MS = 50
  const repeatTo = (unit) => unit.repeat(Math.ceil(KB64 / unit.length)).slice(0, KB64)

  // One near-miss per pattern: the prefix repeats forever and the tail never
  // completes, which is exactly the shape that makes a naive regex backtrack.
  const ADVERSARIAL = [
    ['anthropic', repeatTo('sk-ant-')],
    ['openai', repeatTo('sk-')],
    ['github', repeatTo('ghp_')],
    ['aws_access_key', repeatTo('AKIA')],
    ['aws_secret_key', repeatTo('A')],
    ['slack', repeatTo('xoxb-')],
    ['jwt', repeatTo('eyJ')],
    ['jwt.dotless', `eyJ${'A'.repeat(KB64 - 3)}`],
    ['private_key', repeatTo('-----BEGIN ')],
    ['generic', repeatTo('api_key=')],
    ['all', repeatTo('sk-ant-eyJ.eyJ.AKIA xoxb- ghp_ token= -----BEGIN ')],
  ]

  // MIN OF SEVEN, not one sample. What the 50 ms budget is about is the cost of
  // the SCAN — whether a quantifier backtracks — and a single sample also
  // measures whatever the host did during it. A GC pause on a loaded box put
  // one 0.5 ms scan at 155 ms and failed this gate with the regexes unchanged,
  // which is a gate that teaches people to rerun rather than to look. The
  // minimum is the honest steady-state figure and it still catches the defect
  // this exists for: a quadratic pattern is slow on EVERY sample.
  const SAMPLES = 7
  it.each(ADVERSARIAL)('%s scans a 64 KB input inside the budget', (_name, input) => {
    expect(input.length).toBe(KB64)
    findSecrets(input) // warm the JIT so the measurement is of the scan, not of compilation
    let best = Infinity
    let hits = []
    for (let i = 0; i < SAMPLES; i += 1) {
      const started = performance.now()
      hits = findSecrets(input)
      best = Math.min(best, performance.now() - started)
    }
    expect(hits.length).toBeLessThanOrEqual(MAX_HITS)
    expect(best, `${_name} took ${best.toFixed(2)}ms (min of ${SAMPLES})`).toBeLessThan(BUDGET_MS)
  })
})
