/**
 * /try's version affordances are read-only while a preview is seated
 * (standardization slice 6a).
 *
 * WHY A SOURCE PIN AND NOT A RENDER. The chips live in the Execution tab, and
 * the local proof fixture's head is v1, so `canUndo` is false there whatever
 * the preview state is: an e2e or render assertion on them would pass
 * vacuously, which is worse than no assertion. What the product actually
 * promises is that the SAME term /app has always carried appears in /try's
 * disable expressions, so this file reads the shipped source and says so.
 *
 * The history: PR #409 dropped a read-only-while-previewing assertion and
 * filed the gap; PR #410 folded `previewLocked` into `writeLocked`, which
 * refuses a tool RUN during a preview but left these two affordances live.
 * The behavioural half of the assertion (zero mutating requests during a
 * preview, on BOTH shells) is reinstated in
 * e2e/local/version-restore.spec.mjs.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// Path math on a STRING, not `readFileSync(new URL(...))`: under the jsdom
// environment the global `URL` is jsdom's, and node rejects an instance of it
// with "The URL must be of scheme file" even when the scheme is exactly that.
// Read lazily so a resolution failure surfaces as a failing test rather than a
// dead suite.
const TOOLCAST = join(dirname(fileURLToPath(import.meta.url)), 'ToolCast.jsx')
const source = () => readFileSync(TOOLCAST, 'utf8')

// The two chips, matched on their whole JSX element so a term added to one and
// forgotten on the other cannot pass.
const UNDO = /<button[^>]*onClick=\{undo\}[^>]*disabled=\{([^}]*)\}/
const REDO = /<button[^>]*onClick=\{redo\}[^>]*disabled=\{([^}]*)\}/

describe('/try disables its version affordances while previewing', () => {
  it('the Undo chip is disabled by previewLocked', () => {
    const match = source().match(UNDO)
    expect(match, 'the Undo chip moved; re-point this pin').toBeTruthy()
    expect(match[1]).toContain('previewLocked')
  })

  it('the Redo chip is disabled by previewLocked', () => {
    const match = source().match(REDO)
    expect(match, 'the Redo chip moved; re-point this pin').toBeTruthy()
    expect(match[1]).toContain('previewLocked')
  })

  it('previewLocked still means "a version is seated read-only"', () => {
    // A pin on a name is worthless if the name stops meaning what it says.
    expect(source()).toContain('const previewLocked = drawing.previewing != null')
  })

  it('the probes would catch the term being dropped (positive control)', () => {
    // Without this, a regex that stopped matching would report GREEN forever.
    const dropped = source().replace(/ \|\| previewLocked/g, '')
    expect(dropped.match(UNDO)[1]).not.toContain('previewLocked')
    expect(dropped.match(REDO)[1]).not.toContain('previewLocked')
  })
})
