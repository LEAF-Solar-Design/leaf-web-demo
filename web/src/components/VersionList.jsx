import { useEffect, useState } from 'react'

// THE ONE version-list primitive (standardization slice 6a).
//
// Before this file the version chain was rendered twice, by two files that had
// drifted: /app's VersionHistory.jsx drawer (delta chips, a two-step Restore
// on every non-head row) and /try's inline tab inside ToolCast.jsx (no delta
// chips at all, a Recover action only while the head is unreadable). Two
// renderers meant two answers to the same product question, and a fix landed
// in one was invisible in the other.
//
// This module owns the BEHAVIOUR once: newest-first ordering, the delta chip,
// the authored-tool provenance chip, the two-step restore/recover confirm
// state machine (idle -> confirming -> running -> error), the per-row pending
// label, and the read-only preview strip. It does NOT own shell chrome: each
// surface keeps its own skin, because the drawer and the tab are genuinely
// different furniture and their DOM is pinned by e2e selectors on both sides.
// `variant` selects the skin; every branch below renders the markup that
// surface shipped before this slice, byte for byte, plus the new chips.
//
// HARDENING CONTRACT of this file:
//   * fails closed on bad data — a row with a non-finite `v` is dropped, never
//     rendered with a NaN key or testid;
//   * one restore in flight at a time, enforced here rather than by each
//     caller's own duplicate pending flag (the drift this slice removes);
//   * no allocation per render beyond the single sorted copy of the rows;
//   * `sourceRef` is display-only and is rendered as TEXT, never as a link or
//     an href — it is a digest a server validated, not a URL this file trusts.

// A row whose version number is not a finite number cannot be keyed, cannot be
// previewed and cannot be restored. Dropping it is the fail-closed answer; the
// alternative is a `data-testid="vh-row-vNaN"` that no selector can address.
function usableRows(versions) {
  if (!Array.isArray(versions)) return []
  const rows = []
  for (const row of versions) {
    if (row && Number.isFinite(Number(row.v))) rows.push(row)
  }
  // Newest first. The server returns the chain in ascending manifest order, so
  // this is the same sequence both shells rendered before (the drawer sorted,
  // the tab reversed); sorting is the one that stays correct if the chain ever
  // arrives unordered.
  rows.sort((a, b) => Number(b.v) - Number(a.v))
  return rows
}

// Compact "+a ~m -d" delta chip. Null/undefined `delta` (the root version, or
// a payload either side of the diff failed to parse) renders nothing — the row
// simply carries no chip, per the design note (ship what IS derivable).
export function DeltaChip({ delta }) {
  if (!delta) return null
  const { added = 0, modified = 0, deleted = 0 } = delta
  if (!added && !modified && !deleted) {
    return <span className="vh-delta vh-delta-none" data-testid="vh-delta" title="No entity changes from the parent version">±0</span>
  }
  return (
    <span
      className="vh-delta"
      data-testid="vh-delta"
      title={`${added} added · ${modified} modified · ${deleted} removed (vs. the parent version)`}
    >
      {added > 0 && <span className="vh-delta-add">+{added}</span>}
      {modified > 0 && <span className="vh-delta-mod">~{modified}</span>}
      {deleted > 0 && <span className="vh-delta-del">-{deleted}</span>}
    </span>
  )
}

// Authored-tool provenance. `source_ref` is the sha256 of a
// `leaf.tool-source.v1` receipt over the writing tool's source + manifest; the
// server bounds and charset-validates it before it ships (drawings.py
// `_source_ref`), so anything that reaches here is 64 lowercase hex or null.
// This chip renders ONLY when a row actually carries one — absence means "not
// established", and this file never invents an author for a version.
//
// It says "authored tool", not who or what wrote that tool, because the
// receipt behind the digest carries NO author identity: `ToolSourceReceipt`
// (harness/contract/HARNESS-CONTRACT.md) is exact paths, byte counts and
// digests only. The digest proves "these bytes came from a tool source with
// this digest" and nothing more, so naming a model or a person here would be
// the same invention this surface forbids everywhere else.
export function SourceRefChip({ sourceRef }) {
  if (typeof sourceRef !== 'string' || sourceRef.length === 0) return null
  return (
    <span
      className="vh-source"
      data-testid="vh-source-ref"
      title={`Authored tool receipt ${sourceRef}`}
    >
      authored tool · {sourceRef.slice(0, 8)}
    </span>
  )
}

// The read-only-preview strip both shells show while an older version is
// seated in the viewer. One behaviour (announce the seated version, offer the
// way back to head), two skins pinned by each surface's e2e selectors.
export function VersionPreviewStrip({ variant = 'drawer', version, latest, onBackToHead }) {
  if (version == null) return null
  if (variant === 'tab') {
    return (
      <>
        <div className="tc-preview-note">
          Viewing v{version} read-only
          <button type="button" onClick={onBackToHead}>Back to head</button>
        </div>
        {/* The lock is real (writeLocked folds previewLocked in) — say so here,
            where the operator already is and where the control that lifts it
            sits. A SIBLING, not a child: .tc-preview-note is a two-item
            space-between flex row, and a third child would spread across it.
            Keeping the note's DOM intact also keeps the acceptance driver's
            `getByText(/Viewing v1 read-only/)` a single match. */}
        <div className="tc-preview-lock" role="status" data-testid="try-preview-write-lock">
          Editing is paused until you return to head.
        </div>
      </>
    )
  }
  return (
    <div className="vh-previewing">
      <span>Viewing v{version}{latest != null ? ` of ${latest}` : ''} — read-only preview</span>
      <button className="chip-act" onClick={onBackToHead}>Back to head</button>
    </div>
  )
}

/**
 * The version chain, rendered.
 *
 * @param {'drawer'|'tab'} variant  which surface's skin to render.
 * @param {Array} versions          rows straight off GET /versions.
 * @param {number|null} head        the current head version.
 * @param {number|null} previewingVersion  the version seated read-only, if any.
 * @param {(v:number)=>void} onPreview     seat a version read-only.
 * @param {(row:any)=>string|undefined} rowTitle  optional hover title (the
 *        drawer's absolute timestamp); the primitive never formats dates, so
 *        each shell keeps its own time presentation.
 * @param {(row:any)=>import('react').ReactNode} rowSub  optional secondary
 *        line (the drawer's note/sha/when row).
 * @param {object|null} restore     the restore affordance, or null for a shell
 *        with none. Shape:
 *          { run(v): Promise, mode: 'restore'|'recover',
 *            eligible(row, isHead): boolean, disabled: boolean }
 *        The primitive owns confirm/pending/error; the shell owns the effect.
 */
export default function VersionList({
  variant = 'drawer',
  versions,
  head = null,
  previewingVersion = null,
  onPreview,
  rowTitle,
  rowSub,
  restore = null,
}) {
  // The two-step confirm state machine, owned HERE so both shells answer the
  // same way and a fix lands once. `pending` doubles as the single-flight
  // guard: one restore may be in flight across the whole list.
  const [confirming, setConfirming] = useState(null)
  const [pending, setPending] = useState(null)
  const [failure, setFailure] = useState(null) // {version, message} | null

  // A fresh chain (a real reload, or a drawing switch) retires any stale
  // confirm/error state rather than leaving a confirm prompt pointing at a
  // version that may no longer be in the list.
  useEffect(() => {
    setConfirming(null)
    setFailure(null)
  }, [versions])

  const rows = usableRows(versions)
  // The drawer's controls ship without a `type` attribute and the tab's ship
  // with type="button". Both DOMs are pinned by e2e selectors, so the skin
  // carries the difference rather than the primitive normalizing it.
  const btnType = variant === 'tab' ? 'button' : undefined
  const mode = restore?.mode === 'recover' ? 'recover' : 'restore'
  const verb = mode === 'recover' ? 'Recover' : 'Restore'
  const running = mode === 'recover' ? 'Recovering…' : 'Restoring…'

  async function runRestore(v) {
    if (!restore || pending != null || restore.disabled) return
    setPending(v)
    setFailure(null)
    try {
      await restore.run(v)
      setConfirming(null)
    } catch (cause) {
      setFailure({ version: v, message: cause?.message || `${verb} failed.` })
    } finally {
      setPending(null)
    }
  }

  function restoreControls(v) {
    const isRunning = pending === v
    if (confirming === v) {
      return (
        <>
          {variant === 'drawer' && <span className="confirm-q">Restore v{v} as the new head?</span>}
          <button
            type={btnType}
            className="chip-act"
            disabled={isRunning || pending != null || restore.disabled}
            onClick={() => runRestore(v)}
          >
            {isRunning ? running : `${verb} ${mode === 'recover' ? 'from ' : ''}v${v}`}
          </button>
          <button
            type={btnType}
            className="chip-neutral"
            disabled={pending != null}
            onClick={() => setConfirming(null)}
          >
            Cancel
          </button>
        </>
      )
    }
    return (
      <button
        type={btnType}
        className="chip-act"
        disabled={pending != null || restore.disabled}
        onClick={() => { setFailure(null); setConfirming(v) }}
      >
        {verb}
      </button>
    )
  }

  if (variant === 'tab') {
    return (
      <>
        {failure && <div className="tc-panel-error" role="alert">{failure.message}</div>}
        <div className="tc-version-list">
        {rows.map((row) => {
          const v = Number(row.v)
          const isHead = v === Number(head)
          const showRestore = Boolean(restore && restore.eligible(row, isHead))
          return (
            <div className="tc-version-row" key={v} data-testid={`try-version-v${v}`}>
              <button
                type="button"
                className={previewingVersion === v ? 'active' : ''}
                onClick={() => onPreview(v)}
              >
                <span>v{v}</span>
                <span>{row.tool || 'drawing'}</span>
                {isHead ? <b>head</b> : null}
                <DeltaChip delta={row.delta} />
                <SourceRefChip sourceRef={row.source_ref} />
              </button>
              {showRestore
                ? (confirming === v
                  ? <span className="tc-version-recovery">{restoreControls(v)}</span>
                  : restoreControls(v))
                : null}
            </div>
          )
        })}
        </div>
      </>
    )
  }

  return (
    <ul className="vh-list">
      {rows.map((row) => {
        const v = Number(row.v)
        const isHead = v === Number(head)
        const isPreview = previewingVersion === v
        const showRestore = Boolean(restore && restore.eligible(row, isHead))
        return (
          <li key={v} data-testid={`vh-row-v${v}`}>
            <div className="vh-row-line">
              <button
                className={`vh-row${isPreview ? ' active' : ''}`}
                onClick={() => onPreview(v)}
                title={rowTitle?.(row)}
              >
                <span className="lbar" />
                <span className="vh-main">
                  <span className="vh-row-top">
                    <span className="vh-v">v{v}</span>
                    {row.tool && <span className="vh-tool">{row.tool}</span>}
                    {isHead && <span className="vh-mark">head</span>}
                    <DeltaChip delta={row.delta} />
                    <SourceRefChip sourceRef={row.source_ref} />
                  </span>
                  {rowSub?.(row)}
                </span>
                {isPreview && <span className="key hot">Enter</span>}
              </button>

              {/* Restoring the current head onto itself is a no-op-shaped
                  action the server would still accept; the UI skips it. */}
              {showRestore && (
                <span className="vh-restore" aria-label={`Restore version ${v}`}>
                  {restoreControls(v)}
                </span>
              )}
            </div>
            {failure?.version === v && (
              <div className="field-err vh-restore-err" role="alert">{failure.message}</div>
            )}
          </li>
        )
      })}
    </ul>
  )
}
