import { useCallback, useEffect, useRef, useState } from 'react'
import { getPlatformChange, landPlatformChange, mergePlatformChange, proposePlatformChange } from '../api.js'
import './popovers.css'

// Platform self-edit drawer (R7 admin lane) — visible only with ?customize=1
// AND a policy read that carries platform_customize: true (strict; App gates
// the mount). A DT2 right drawer like OpsDrawer: propose branch-only edits to
// the platform repo, watch the change record, land the approved commit into
// the standing PR pipeline. The co-sign approve/deny step is deliberately
// ABSENT here: it authenticates with an independent secret that must never
// enter the browser, so awaiting_cosign renders as a calm hold, not a button.
// `exiting`: App holds the mount through the 180 ms M1 exit fade (useExit).

const RECENT_BASE_KEY = 'leaf.customize.recent'
const MAX_RECENT = 8
// Mirror of the server's lane.MAX_EDITS (routers/platform_customize.py caps the
// propose body at 200 edits): the composer must not build a request the
// contract will refuse.
const MAX_EDITS = 200
const CHANGE_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

const REASON_COPY = {
  policy_unavailable: 'The entitlements policy is unavailable right now. Try again shortly.',
  edit_path_invalid: 'A path is not allowed. Paths are repo-relative with forward slashes; no leading slash, no "..", no ".git".',
  platform_customize_auth_required: 'Live authentication is required on this environment.',
  tenant_identity_invalid: 'The signed-in tenant identity is not valid here.',
  platform_customize_disabled: 'The self-edit lane is dark on this environment.',
  title_invalid: 'Title must be 1 to 200 characters.',
  edits_invalid: 'One of the edits is not acceptable. Check paths and contents.',
  edits_noop: 'Those edits match the base exactly. Nothing to propose.',
  change_id_invalid: 'That change id is not a valid id.',
  change_not_found: 'No change with that id is visible to this tenant.',
  land_ack_mismatch: 'The acknowledged commit is not the recorded commit. Refresh the status.',
  cosign_required: 'Fundamental paths need the independent co-sign before landing.',
  change_not_landable: 'This change was denied. It cannot land.',
  branch_diverged: 'The lane branch moved since proposal. Propose again.',
  change_conflict: 'A concurrent update touched this change. Refresh the status.',
  platform_repo_unavailable: 'The platform repo is not available to the server.',
  change_record_unavailable: 'The change record store is unavailable.',
  fundamental_manifest_unavailable: 'The fundamental-paths manifest is unavailable.',
  platform_customize_failed: 'The lane hit an unexpected failure. Try again.',
}

function describeError(e) {
  // The entitlement_required marker rides BOTH the 403 denial and the 503
  // policy-unavailable envelope (which carries NO reason_code): 403 means
  // "you lack the tier", anything else with the marker is the fail-closed
  // policy outage and must read as retryable, not as missing access.
  if (e?.body?.entitlement_required) {
    if (e.status === 403) return 'Admin tier required for platform self-edit.'
    return REASON_COPY.policy_unavailable
  }
  const reason = e?.body?.reason_code
  if (reason && REASON_COPY[reason]) return REASON_COPY[reason]
  return String(e?.message || e)
}

// Recent ids are scoped per tenant so a multi-workspace admin never sees one
// workspace's change ids from another (the server record itself is
// tenant-scoped regardless; this is display hygiene, not authority).
function recentKey(tenant) {
  return `${RECENT_BASE_KEY}.${tenant || 'default'}`
}

function readRecent(tenant) {
  try {
    const raw = JSON.parse(localStorage.getItem(recentKey(tenant)) || '[]')
    if (!Array.isArray(raw)) return []
    // Coerce on READ, not just write: stored rows are untrusted (any code on
    // the origin can write them), so a non-string title must not reach React.
    return raw
      .filter((r) => r && typeof r.change_id === 'string' && CHANGE_ID_RE.test(r.change_id))
      .map((r) => ({ change_id: r.change_id, title: typeof r.title === 'string' ? r.title : '' }))
  } catch { return [] }
}

function rememberRecent(tenant, record) {
  try {
    const next = [{ change_id: record.change_id, title: String(record.title || '') }]
      .concat(readRecent(tenant).filter((r) => r.change_id !== record.change_id))
      .slice(0, MAX_RECENT)
    localStorage.setItem(recentKey(tenant), JSON.stringify(next))
    return next
  } catch { return readRecent(tenant) }
}

// State chips reuse the app's status-dot grammar: a hold is amber (quota),
// never red; denied is the one genuinely red terminal state.
const STATE_LABEL = {
  approved: ['done', 'Approved'],
  awaiting_cosign: ['quota', 'Awaiting co-sign'],
  denied: ['red', 'Denied'],
  landed: ['done', 'Landed'],
  merged: ['done', 'Merged'],
}

const EMPTY_EDIT = { path: '', content: '', delete: false }

export default function CustomizePanel({ onDismiss, exiting, tenant }) {
  const drawerRef = useRef(null)
  const closeRef = useRef(null)
  const restoreRef = useRef(null)
  const [title, setTitle] = useState('')
  const [edits, setEdits] = useState([{ ...EMPTY_EDIT }])
  const [submitting, setSubmitting] = useState(false)
  const [record, setRecord] = useState(null)
  const [err, setErr] = useState(null)
  const [recent, setRecent] = useState(() => readRecent(tenant))
  // The tenant echo arrives asynchronously after mount (App learns it from
  // /api/session), so re-read the tenant-scoped bucket whenever it changes;
  // otherwise the panel would keep showing (and writing) the default bucket.
  // The ref carries the LATEST tenant into async completions: a proposal that
  // started before the echo landed must record into the bucket that is
  // current when its response arrives, not the one its closure captured.
  const tenantRef = useRef(tenant)
  useEffect(() => { tenantRef.current = tenant; setRecent(readRecent(tenant)) }, [tenant])
  const [refreshing, setRefreshing] = useState(false)
  const [landConfirming, setLandConfirming] = useState(false)
  const [landing, setLanding] = useState(false)
  const [mergeConfirming, setMergeConfirming] = useState(false)
  const [merging, setMerging] = useState(false)

  useEffect(() => {
    restoreRef.current = document.activeElement
    closeRef.current?.focus()
    return () => {
      const target = restoreRef.current
      if (target?.isConnected && typeof target.focus === 'function') target.focus()
    }
  }, [])

  // Own Escape before the scene-level ladder can navigate away.
  useEffect(() => {
    if (!onDismiss) return
    const onKey = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopImmediatePropagation()
      onDismiss()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onDismiss])

  const ownKeyboard = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      onDismiss?.()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = [...(drawerRef.current?.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])') || [])]
      .filter((element) => element.getClientRects().length > 0)
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (focusable.length === 1 || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault()
      first.focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    }
  }

  const setEdit = (index, patch) => {
    setEdits((prev) => prev.map((e, i) => (i === index ? { ...e, ...patch } : e)))
  }

  const propose = useCallback(async () => {
    const cleaned = edits
      .map((e) => ({ path: e.path.trim(), content: e.content, delete: !!e.delete }))
      .filter((e) => e.path)
    if (!title.trim() || !cleaned.length) {
      setErr('A title and at least one edit with a path are required.')
      return
    }
    setSubmitting(true); setErr(null); setLandConfirming(false)
    try {
      const body = cleaned.map((e) => (e.delete
        ? { path: e.path, delete: true }
        : { path: e.path, content: e.content }))
      const created = await proposePlatformChange(title.trim(), body)
      setRecord(created)
      setRecent(rememberRecent(tenantRef.current, created))
    } catch (e) {
      setErr(describeError(e))
    } finally {
      setSubmitting(false)
    }
  }, [title, edits])

  const refresh = useCallback(async (changeId) => {
    const id = changeId || record?.change_id
    if (!id) return
    setRefreshing(true); setErr(null); setLandConfirming(false)
    try {
      setRecord(await getPlatformChange(id))
    } catch (e) {
      setErr(describeError(e))
    } finally {
      setRefreshing(false)
    }
  }, [record])

  const land = useCallback(async () => {
    if (!record?.change_id || !record?.commit_sha) return
    setLanding(true); setErr(null)
    try {
      // The land ack is the EXACT recorded commit — never a re-read of the
      // mutable branch. The server enforces the same equality (409 on drift).
      setRecord(await landPlatformChange(record.change_id, record.commit_sha))
      setLandConfirming(false)
    } catch (e) {
      setErr(describeError(e))
    } finally {
      setLanding(false)
    }
  }, [record])

  const merge = useCallback(async () => {
    if (!record?.change_id || !record?.commit_sha) return
    setMerging(true); setErr(null)
    try {
      // The merge ack is the EXACT recorded commit, same discipline as the
      // land ack; the server re-verifies review PASS + open PR + unmoved
      // head, then GitHub's sha-pinned merge refuses moved bytes.
      setRecord(await mergePlatformChange(record.change_id, record.commit_sha))
      setMergeConfirming(false)
    } catch (e) {
      setErr(describeError(e))
    } finally {
      setMerging(false)
    }
  }, [record])

  const state = record?.state
  const stateChip = STATE_LABEL[state] || ['', state || '']
  const fundamentals = record?.fundamental_paths || []

  return (
    <aside ref={drawerRef} className={`drawer${exiting ? ' exit' : ''}`} role="dialog" aria-modal="true" aria-label="Platform self-edit" onKeyDown={ownKeyboard}>
      <div className="drawer-head">
        <span className="ops-badge">Admin</span>
        <span className="drawer-title">Platform · self-edit</span>
        {onDismiss && (
          <button ref={closeRef} className="key hot" onClick={onDismiss} aria-label="Hide drawer">Esc</button>
        )}
      </div>

      <div className="drawer-body">
        <section aria-labelledby="cst-propose-title">
          <div className="ops-section-head">
            <div>
              <div id="cst-propose-title" className="ops-section-title">Propose a change</div>
              <div className="ops-section-copy">Branch-only edits to the platform repo · merging needs your fresh approval of the exact commit · nothing deploys from here</div>
            </div>
          </div>

          <label className="param">
            Title
            <input
              value={title}
              maxLength={200}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="What this change does"
            />
          </label>

          {edits.map((edit, index) => (
            <div className="cst-edit" key={index}>
              <label className="param">
                Path {index + 1}
                <input
                  value={edit.path}
                  onChange={(e) => setEdit(index, { path: e.target.value })}
                  placeholder="web/src/… (repo-relative, forward slashes)"
                />
              </label>
              <label className="cst-check">
                <input
                  type="checkbox"
                  checked={edit.delete}
                  onChange={(e) => setEdit(index, { delete: e.target.checked })}
                />
                Delete this file
              </label>
              {!edit.delete && (
                <label className="param">
                  Content (full file)
                  <textarea
                    rows={6}
                    value={edit.content}
                    onChange={(e) => setEdit(index, { content: e.target.value })}
                    placeholder="Entire new file content"
                  />
                </label>
              )}
              {edits.length > 1 && (
                <button className="chip-neutral" onClick={() => setEdits((prev) => prev.filter((_, i) => i !== index))}>
                  Remove edit {index + 1}
                </button>
              )}
            </div>
          ))}

          <div className="cst-actions">
            <button
              className="chip-neutral"
              onClick={() => setEdits((prev) => (prev.length >= MAX_EDITS ? prev : [...prev, { ...EMPTY_EDIT }]))}
              disabled={edits.length >= MAX_EDITS}
            >
              {edits.length >= MAX_EDITS ? `At the ${MAX_EDITS}-edit limit` : 'Add another edit'}
            </button>
            <button className="chip-act" onClick={propose} disabled={submitting}>
              {submitting ? 'Proposing…' : 'Propose'}
            </button>
          </div>
        </section>

        {err && (
          <div className="pane-fail" role="alert">
            <span className="pane-fail-title"><span className="dot red" />Not accepted</span>
            <span className="pane-fail-reason">{err}</span>
          </div>
        )}

        {record && (
          <section aria-labelledby="cst-record-title">
            <div className="ops-section-head">
              <div>
                <div id="cst-record-title" className="ops-section-title">Change record</div>
                <div className="ops-section-copy">{record.title}</div>
              </div>
            </div>
            <div className="cst-meta">
              <span className={`state ${stateChip[0]}`}>{stateChip[1]}</span>
              <span className="cst-mono">{record.branch}</span>
              <span className="cst-mono">{String(record.commit_sha || '').slice(0, 12)}</span>
            </div>

            {state === 'awaiting_cosign' && (
              <div className="ent-note amber">
                Fundamental paths need an independent co-sign before landing:
                {' '}{fundamentals.join(', ')}. That approval happens outside the
                browser; refresh here once it is recorded.
              </div>
            )}
            {state === 'merged' && record.merge && (
              <div className="ops-note">
                Merged into main
                {record.merge.merge_commit_sha ? ` as ${String(record.merge.merge_commit_sha).slice(0, 12)}` : ''}.
                The staging canary is the next stage; deploys stay outside this panel.
              </div>
            )}
            {state === 'landed' && record.push && (
              <div className="ops-note">
                {!record.push.pushed
                  ? 'Landed locally. Push is disabled on this environment, so the branch stays on the server until an operator collects it.'
                  : record.pr && typeof record.pr.url === 'string' && record.pr.url.startsWith('https://')
                    ? (
                      <>
                        Branch pushed to {record.push.remote}.{' '}
                        <a href={record.pr.url} target="_blank" rel="noopener noreferrer">
                          Pull request #{record.pr.number}
                        </a>
                        {' '}
                        {record.review?.pr_state === 'merged'
                          ? 'is merged.'
                          : record.review?.pr_state === 'closed'
                            ? 'was closed without merging.'
                            : record.review?.state === 'passed'
                              ? 'is open. Review: PASS — the merge gate is next.'
                              : record.review?.state === 'failed'
                                ? `is open. Review: RED${record.review.description ? ` (${record.review.description})` : ''} — a fix round runs before anything merges.`
                                : record.review?.state === 'error_verdict'
                                  ? 'is open. The review produced no readable verdict; it will be re-run.'
                                  : record.review?.state === 'pending'
                                    ? 'is open. Review: dispatched, awaiting the verdict.'
                                    : 'is open. Review: awaiting dispatch.'}
                      </>
                    )
                    : record.pr && record.pr.error
                      ? `Branch pushed to ${record.push.remote}, but the pull request could not be opened automatically (${record.pr.error}). Land again to retry, or open it manually.`
                      : `Branch pushed to ${record.push.remote}. Next: pull request, review gate, merge, staging canary.`}
              </div>
            )}

            <div className="cst-actions">
              <button className="chip-neutral" onClick={() => refresh()} disabled={refreshing}>
                {refreshing ? 'Refreshing…' : 'Refresh status'}
              </button>
              {state === 'approved' && (
                landConfirming ? (
                  <span className="ops-confirm">
                    <span className="confirm-q">Land commit {String(record.commit_sha || '').slice(0, 12)}?</span>
                    <button className="chip-act" onClick={land} disabled={landing}>
                      {landing ? 'Landing…' : 'Land'}
                    </button>
                    <button className="chip-neutral" onClick={() => setLandConfirming(false)} disabled={landing}>
                      Keep
                    </button>
                  </span>
                ) : (
                  <button className="chip-act" onClick={() => setLandConfirming(true)}>Land</button>
                )
              )}
              {state === 'landed' && record.review?.state === 'passed' && record.review?.pr_state === 'open' && (
                mergeConfirming ? (
                  <span className="ops-confirm">
                    <span className="confirm-q">Merge commit {String(record.commit_sha || '').slice(0, 12)} into main?</span>
                    <button className="chip-act" onClick={merge} disabled={merging}>
                      {merging ? 'Merging…' : 'Merge'}
                    </button>
                    <button className="chip-neutral" onClick={() => setMergeConfirming(false)} disabled={merging}>
                      Keep
                    </button>
                  </span>
                ) : (
                  <button className="chip-act" onClick={() => setMergeConfirming(true)}>Merge</button>
                )
              )}
            </div>
          </section>
        )}

        {recent.length > 0 && (
          <section aria-labelledby="cst-recent-title">
            <div className="ops-section-head">
              <div>
                <div id="cst-recent-title" className="ops-section-title">Recent changes</div>
                <div className="ops-section-copy">Kept in this browser only · the server keeps the durable record</div>
              </div>
            </div>
            <ul className="cst-recent">
              {recent.map((r) => (
                <li key={r.change_id}>
                  <button className="chip-neutral" onClick={() => refresh(r.change_id)} disabled={refreshing}>
                    <span className="cst-mono">{r.change_id.slice(0, 8)}</span> · {r.title || 'untitled'}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      <div className="drawer-foot">Branch-only writes · landing hands off to the standing PR pipeline · co-sign authority never enters the browser</div>
    </aside>
  )
}
