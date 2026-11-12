import { useEffect, useRef, useState } from 'react'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { listProjectReferences } from '../projects/api.js'
import {
  isSourceUuid, listProjectSourceRequests, readProjectSourceRequest, stageProjectSource,
  confirmProjectSource, recoverProjectSource, rejectProjectSource, exportProjectSourceBundle, validateProjectSource,
} from '../api.js'
import { isSecretRefused } from '../lib/secretGuardTransport.js'
import { exportProjectNativeSourceBundle, loadProjectNativePreview, submitProjectSourceNativePreview, validateNativePreview } from './projectPageClient.js'

const LIMIT = 8 * 1024 * 1024
const pending = (row) => ['staged', 'awaiting_confirmation', 'publishing'].includes(row?.state)
const freshState = () => ({ open: false, requirements: '', materials: [], selected: [], materialsComplete: false,
  selectionReviewed: false, candidates: [], candidate: null, intent: null, busy: false, loading: false, sourceLoaded: false,
  error: null, secretRefusal: null, unknown: false, materialsLoading: false, unsupportedInputs: [],
  exportBusy: false, download: null, previewJob: null, previewBusy: false, previewUnknown: false, previewError: null, previewBlocked: false })
const previewWitness = (row) => ({ source_request_id: row.source_request_id, version: row.version,
  receipt_digest: row.receipt_digest, source_commit: row.source_commit, source_tree: row.source_tree })
const samePreviewSource = (source, row) => row && Object.entries(previewWitness(row)).every(([key, value]) => source?.[key] === value)
const previewExpected = (source) => ({ schema: 'leaf.project-repository-preview-source.v1', source_request_id: source.source_request_id,
  source_version: source.version, receipt_digest: source.receipt_digest, source_commit: source.source_commit, source_tree: source.source_tree })
function stopPreview(owner) {
  if (!owner) return
  owner.previewAbort?.abort()
  owner.previewEpoch = (owner.previewEpoch || 0) + 1
  owner.previewLocked = false
}
function discardDownload(owner) {
  if (!owner) return
  owner.exportEpoch = (owner.exportEpoch || 0) + 1
  if (owner.downloadUrl) URL.revokeObjectURL(owner.downloadUrl)
  owner.downloadUrl = null
}
export function sourceMaterialProblem(row, projectId, orgId, sourceContext = 'browser') {
  if (!['browser', 'native'].includes(sourceContext)) return 'Source context is unavailable.'
  if (!row || !isSourceUuid(row.material_id) || row.project_id !== projectId || row.org_id !== orgId
    || !isSourceUuid(row.actor_binding_id) || typeof row.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.sha256)
    || !Number.isInteger(row.byte_size) || row.byte_size < 0 || typeof row.name !== 'string') return 'Reference metadata is unavailable.'
  if (!['text/plain', 'application/json'].includes(row.media_type)) return 'Retained project material; unavailable for this source adapter.'
  if (sourceContext === 'native' && !row.name.trim()) return 'Reference metadata is unavailable.'
  if (sourceContext === 'native' && (row.byte_size < 1 || row.byte_size > 1048576)) return 'Choose a nonempty TXT or JSON original of at most 1 MiB.'
  if (row.byte_size > LIMIT) return 'This original exceeds the 8 MiB source limit.'
  return null
}
export function sourceSelectionProblem(materials, selected, projectId, orgId, sourceContext = 'browser', operation = 'create') {
  if (!['browser', 'native'].includes(sourceContext)) return 'Source context is unavailable.'
  if (sourceContext === 'native' && operation !== 'edit' && selected.length === 0) return 'Choose one through eight TXT or JSON originals.'
  if (selected.length > 8) return 'Choose at most eight originals.'
  const rows = selected.map((id) => materials.find((row) => row.material_id === id))
  if (new Set(selected).size !== selected.length || rows.some((row) => sourceMaterialProblem(row, projectId, orgId, sourceContext))) return 'Review the selected originals; some are unavailable.'
  if (sourceContext === 'native' && rows.reduce((total, row) => total + row.byte_size, 0) > 4194304) return 'Selected originals must total at most 4 MiB.'
  if (rows.reduce((total, row) => total + row.byte_size, 0) > LIMIT) return 'Selected originals must total at most 8 MiB.'
  return null
}
function sourceBaseCommit(candidates, projectId, sourceContext) {
  return candidates.filter((row) => row.state === 'published' && row.project_id === projectId
    && row.schema === `leaf.${sourceContext}-project-source.v1`)
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))[0]?.source_commit
}
function uuid() {
  const value = globalThis.crypto?.randomUUID?.()
  if (!isSourceUuid(value)) throw new Error('Secure request creation is unavailable in this browser.')
  return value
}

// All state here is presentation. Durable candidates are always read from the server.
export default function useProjectSourceController({ projectId, orgId, signedIn, mock, startAgentTurn, sourceContext = 'browser' }) {
  const [, accountChanged] = useState(0)
  useEffect(() => subscribeActiveScope(() => {
    const previous = live.current
    if (previous && readActiveScope() !== previous.accountScope) {
      previous.abort.abort()
      stopPreview(previous)
      discardDownload(previous)
      live.current = null
    }
    accountChanged((value) => value + 1)
  }), [])
  const accountScope = readActiveScope()
  const key = JSON.stringify([projectId, orgId, accountScope, !!signedIn, !!mock, sourceContext])
  const live = useRef(null)
  if (live.current?.key !== key) {
    live.current?.abort.abort()
    stopPreview(live.current)
    discardDownload(live.current)
    live.current = { key, projectId, orgId, accountScope, signedIn, mock, sourceContext,
      abort: new AbortController(), locked: false, sequence: 0, state: freshState() }
  }
  const owner = live.current
  owner.startAgentTurn = startAgentTurn
  const [view, setView] = useState(() => ({ owner, ...owner.state }))
  const current = () => live.current === owner && readActiveScope() === owner.accountScope && !owner.abort.signal.aborted
  const publish = (patch) => {
    if (!current()) return
    owner.state = { ...owner.state, ...patch }
    setView({ owner, ...owner.state })
  }
  useEffect(() => {
    if (owner.abort.signal.aborted) owner.abort = new AbortController()
    if (!live.current) live.current = owner
    return () => { owner.abort.abort(); stopPreview(owner); discardDownload(owner); if (live.current === owner) live.current = null }
  }, [owner])
  const available = () => {
    if (!['browser', 'native'].includes(owner.sourceContext)) return 'Source context is unavailable.'
    if (!owner.signedIn || owner.mock || !owner.accountScope) return 'Source authoring needs a signed-in live account.'
    if (!isSourceUuid(owner.projectId)) return 'Open or create a project through the project controls first.'
    return null
  }
  const options = () => ({ expectedScope: owner.accountScope, signal: owner.abort.signal, sourceContext: owner.sourceContext })
  const clearDownload = () => { discardDownload(owner); publish({ download: null }) }
  const accept = (row) => {
    const previous = owner.state.candidates.find((item) => item.source_request_id === row.source_request_id)
    const differs = (key) => JSON.stringify(row[key]) !== JSON.stringify(previous?.[key])
    if (previous && (row.version < previous.version
      || ['receipt_digest', 'base_commit', 'source_commit', 'source_tree', 'changed_paths', 'created_at'].some(differs)
      || (row.version === previous.version && Object.keys(row).some(differs)))) {
      throw new Error('Project source readback is stale. Refresh before continuing.')
    }
    return row
  }
  const readBack = async (requestId, sequence = owner.sequence) => {
    const row = await readProjectSourceRequest(owner.projectId, requestId, options())
    if (!current() || sequence !== owner.sequence) return null
    accept(row)
    const candidates = [row, ...owner.state.candidates.filter((item) => item.source_request_id !== requestId)]
    publish({ candidates, candidate: row, unknown: false, error: null, sourceLoaded: true })
    restorePreview(row)
    return row
  }
  const refresh = async () => {
    if (owner.locked) return
    clearDownload()
    stopPreview(owner)
    const issue = available()
    if (issue) { publish({ error: issue }); return }
    const sequence = ++owner.sequence
    publish({ loading: true, error: null, sourceLoaded: false, previewBusy: false, previewJob: null, previewUnknown: false, previewError: null })
    try {
      const rows = await listProjectSourceRequests(owner.projectId, options())
      if (!current() || sequence !== owner.sequence) return
      rows.forEach(accept)
      rows.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
      const previousId = owner.state.intent?.source_request_id || owner.state.candidate?.source_request_id || rememberedPreviewRequest()
      const previous = rows.find((row) => row.source_request_id === previousId)
      const candidate = (owner.state.unknown || pending(previous) ? previous : null) || rows.find(pending) || previous || rows[0] || null
      publish({ candidates: rows, candidate, sourceLoaded: true })
      if (owner.state.intent && (owner.state.unknown || !candidate || candidate.source_request_id === owner.state.intent.source_request_id)) await readBack(owner.state.intent.source_request_id)
      else { publish({ unknown: false }); restorePreview(candidate) }
    } catch {
      if (current() && sequence === owner.sequence) publish({ error: 'Source readback is unavailable. Keep the original request and refresh.',
        unknown: !!owner.state.intent, candidate: null })
    } finally {
      if (sequence === owner.sequence) publish({ loading: false })
    }
  }
  const loadMaterials = async () => {
    if (owner.state.materialsLoading) return
    const preserveSelection = owner.state.materialsComplete
    publish({ materialsLoading: true })
    const rows = [], cursors = new Set(), ids = new Set()
    let cursor
    try {
      for (let page = 0; page < 20; page += 1) {
        const result = await listProjectReferences(owner.projectId, { ...options(), cursor })
        if (!current()) return
        if (!Array.isArray(result?.materials) || rows.length + result.materials.length > 400) throw new Error()
        for (const row of result.materials) {
          if (!row || ids.has(row.material_id)) throw new Error()
          ids.add(row.material_id); rows.push(row)
        }
        if (result.next_cursor === null || result.next_cursor === undefined) {
          const supported = rows.filter((row) => !sourceMaterialProblem(row, owner.projectId, owner.orgId, owner.sourceContext)).map((row) => row.material_id)
          const automatic = sourceSelectionProblem(rows, supported, owner.projectId, owner.orgId, owner.sourceContext) ? [] : supported
          const invalid = rows.some((row) => sourceMaterialProblem(row, owner.projectId, owner.orgId, owner.sourceContext) === 'Reference metadata is unavailable.'
            || (['text/plain', 'application/json'].includes(row.media_type) && sourceMaterialProblem(row, owner.projectId, owner.orgId, owner.sourceContext)))
          publish({ materials: rows, materialsComplete: true,
            ...(preserveSelection ? {} : { selected: invalid ? [] : automatic,
              selectionReviewed: !invalid && supported.length === automatic.length
                && (owner.sourceContext !== 'native' || automatic.length > 0) }) })
          return
        }
        if (!isSourceUuid(result.next_cursor) || cursors.has(result.next_cursor)) throw new Error()
        cursors.add(result.next_cursor); cursor = result.next_cursor
      }
      throw new Error()
    } catch {
      publish({ materials: rows, materialsComplete: false, selected: [], selectionReviewed: false })
    } finally {
      publish({ materialsLoading: false })
    }
  }
  const open = (requirements, options = {}) => {
    const unsupportedInputs = Array.isArray(options?.images) ? options.images.map((image) =>
      typeof image?.file?.name === 'string' && image.file.name ? image.file.name : 'Pasted image') : []
    publish({ open: true, ...(typeof requirements === 'string' && !owner.state.unknown && !owner.state.candidates.some(pending)
      ? { requirements, unsupportedInputs } : {}) })
    const issue = available()
    if (issue) { publish({ error: issue }); return true }
    void refresh()
    if (!owner.state.materialsComplete) void loadMaterials()
    return true
  }
  const mutate = async (action, { allowSecretOnce = false, witness } = {}) => {
    if (owner.locked || owner.exportLocked || !current()) return
    clearDownload()
    owner.locked = true // Click fencing must precede React's next render.
    let sent = false, minting = false, requestId
    publish({ busy: true, error: null, secretRefusal: null })
    try {
      const issue = available()
      if (issue) throw new Error(issue)
      if (owner.state.loading || !owner.state.sourceLoaded) throw new Error('Refresh source readback before continuing.')
      let body, text, hint
      if (action === 'stage') {
        const base = sourceBaseCommit(owner.state.candidates, owner.projectId, owner.sourceContext)
        const operation = base ? 'edit' : 'create'
        const requirements = owner.state.requirements
        if (!requirements.trim() || requirements.includes('\0') || new TextEncoder().encode(requirements).length > 16384) throw new Error('Enter original requirements of at most 16 KiB.')
        if (owner.state.loading || owner.state.materialsLoading || !owner.state.sourceLoaded || !owner.state.materialsComplete) throw new Error('Source or reference readback is incomplete or unavailable. Refresh it before staging.')
        const selectionIssue = sourceSelectionProblem(owner.state.materials, owner.state.selected, owner.projectId, owner.orgId, owner.sourceContext, operation)
        if (selectionIssue || !owner.state.selectionReviewed) throw new Error(selectionIssue || 'Review the reference selection before staging.')
        if (owner.state.unknown || owner.state.candidates.some(pending)) throw new Error('Refresh and review the existing source request first.')
        requestId = uuid()
        const material_refs = owner.state.selected.map((id) => {
          const row = owner.state.materials.find((item) => item.material_id === id)
          return { material_id: row.material_id, sha256: row.sha256 }
        })
        body = { source_request_id: requestId, requirements, source_operation: operation, material_refs,
          ...(base ? { expected_base_commit: base } : {}) }
        text = requirements
        hint = { source: 'author_panel', purpose: 'stage_authority', browser_action: body.source_operation,
          source_request_id: requestId, material_refs, ...(base ? { expected_base_commit: base } : {}) }
      } else {
        const row = owner.state.candidate
        if (owner.state.unknown || !row || witness?.receipt_digest !== row.receipt_digest || witness?.version !== row.version
          || witness?.source_request_id !== row.source_request_id) throw new Error('The displayed source changed. Refresh and review it again.')
        const allowed = action === 'recover' ? row.state === 'publishing' : ['staged', 'awaiting_confirmation'].includes(row.state)
        if (!allowed) throw new Error('This action is unavailable for the current source state.')
        requestId = row.source_request_id
        body = { receipt_digest: row.receipt_digest, expected_version: row.version,
          ...(action === 'confirm' ? { confirmation_id: uuid() } : {}) }
        hint = { source: 'author_panel', purpose: 'stage_authority', browser_action: action, source_request_id: requestId, ...body }
        text = { confirm: 'Confirm these source changes.', recover: 'Recover this source publication.', reject: 'Reject these source changes.' }[action]
      }
      if (owner.sourceContext === 'native') hint = { ...hint, source_profile: 'ios-swiftui-xcodegen-v1' }
      minting = true
      const authority = await owner.startAgentTurn(text, hint, { allowSecretOnce, requireImmediateTurn: true })
      minting = false
      if (!current()) return
      if (!authority?.session_id || !authority?.turn_id) throw new Error('Could not start this source action. Wait for the current conversation turn to finish.')
      const intent = action === 'stage' ? { ...body, referenceNames: owner.state.selected.map((id) => owner.state.materials.find((row) => row.material_id === id).name) }
        : owner.state.intent || { source_request_id: requestId }
      publish({ intent })
      sent = true
      const opts = { ...options(), authority, allowSecretOnce,
        ...(action === 'stage' && owner.sourceContext === 'native'
          ? { materialMetadata: owner.state.selected.map((id) => owner.state.materials.find((row) => row.material_id === id)) } : {}) }
      const result = action === 'stage' ? await stageProjectSource(owner.projectId, body, opts)
        : await ({ confirm: confirmProjectSource, recover: recoverProjectSource, reject: rejectProjectSource }[action])(owner.projectId, requestId, body, opts)
      if (current()) {
        // The mutation's durable witnesses also fence a lagging subsequent read.
        // Keep them for comparison without announcing success before readback.
        accept(result)
        publish({ candidates: [result, ...owner.state.candidates.filter((item) => item.source_request_id !== requestId)] })
        await readBack(requestId)
      }
    } catch (error) {
      if (!current()) return
      if (isSecretRefused(error)) publish({ error: error.message, secretRefusal: error.refusal })
      else if (sent) {
        publish({ unknown: true, candidate: null, error: 'Publication outcome unknown. Refresh the original request; do not stage it again.' })
        try { await readBack(requestId) } catch { /* No repeat POST or editor on an unknown outcome. */ }
      } else publish({ error: minting ? 'Could not start this source action. Wait for the current conversation turn to finish.'
        : error?.message || 'This source action is unavailable.' })
    } finally {
      owner.locked = false
      publish({ busy: false })
    }
  }
  const prepareDownload = async (witness) => {
    if (owner.exportLocked || owner.locked || !current()) return
    clearDownload()
    owner.exportLocked = true // Fence duplicate clicks before React renders.
    const epoch = owner.exportEpoch
    const sequence = owner.sequence
    const row = owner.state.candidate && { ...owner.state.candidate }
    const matches = () => {
      const selected = owner.state.candidate
      return current() && owner.state.open && epoch === owner.exportEpoch && sequence === owner.sequence
        && !owner.state.unknown && !owner.state.loading && owner.state.sourceLoaded
        && row?.state === 'published' && selected?.state === 'published'
        && ['source_request_id', 'version', 'receipt_digest', 'source_commit', 'source_tree'].every((key) => selected[key] === row[key])
    }
    publish({ exportBusy: true, error: null })
    try {
      if (available() || !matches() || !witness
        || ['source_request_id', 'version', 'receipt_digest'].some((key) => witness[key] !== row[key])) throw new Error('Refresh and review the saved source before downloading.')
      validateProjectSource(row, owner.projectId, undefined, owner.sourceContext)
      const exportBundle = owner.sourceContext === 'native' ? exportProjectNativeSourceBundle : exportProjectSourceBundle
      const result = await exportBundle(owner.projectId, row, options())
      if (!matches()) return
      owner.downloadUrl = URL.createObjectURL(result.blob)
      publish({ download: { ...result, blob: undefined, url: owner.downloadUrl } })
    } catch (error) {
      if (current() && epoch === owner.exportEpoch) {
        clearDownload()
        publish({ error: error?.message || 'Project source download is unavailable.' })
      }
    } finally {
      owner.exportLocked = false
      publish({ exportBusy: false })
    }
  }
  const previewStorageKey = (row) => `leaf.source-native-preview.v1:${owner.key}:${row.source_request_id}`
  const previewPointerKey = () => `leaf.source-native-preview.v1:${owner.key}:active`
  const rememberedPreviewRequest = () => {
    if (owner.sourceContext !== 'native') return null
    try {
      const id = localStorage.getItem(previewPointerKey())
      return isSourceUuid(id) ? id : null
    } catch { return null }
  }
  const savePreview = (record) => {
    // Save the retry identity before sending; a reload must never mint a second job.
    localStorage.setItem(previewStorageKey(record.source), JSON.stringify(record))
    localStorage.setItem(previewPointerKey(), record.source.source_request_id)
    owner.previewRecord = record
  }
  const observePreview = async () => {
    const record = owner.previewRecord
    if (owner.previewLocked || !current() || !owner.state.open || owner.sourceContext !== 'native'
      || !record || !samePreviewSource(record.source, owner.state.candidate) || owner.state.previewBlocked) return
    owner.previewLocked = true
    owner.previewAbort = new AbortController()
    const epoch = owner.previewEpoch || 0
    const matches = () => current() && owner.state.open && epoch === (owner.previewEpoch || 0)
      && samePreviewSource(record.source, owner.state.candidate)
    publish({ previewBusy: true, previewError: null })
    try {
      const opts = { expectedScope: owner.accountScope, signal: owner.previewAbort.signal }
      const job = record.job
        ? await loadProjectNativePreview(owner.projectId, record.job.job_id, { ...opts,
          expected: { ...previewExpected(record.source), repo_key: record.job.repo_key, archive_sha256: record.job.archive_sha256 } })
        : await submitProjectSourceNativePreview(owner.projectId, record.source, record.key, opts)
      if (!matches()) return
      if (record.job && ((['complete', 'failed', 'cancelled'].includes(record.job.status) && job.status !== record.job.status)
        || (record.job.status === 'running' && job.status === 'queued'))) throw new Error('Stale preview state.')
      savePreview({ ...record, job })
      publish({ previewJob: job, previewUnknown: false })
    } catch {
      if (matches()) publish({ previewUnknown: true,
        previewError: 'Preview outcome unknown. Check the existing preview before starting another.' })
    } finally {
      if (matches()) { owner.previewLocked = false; publish({ previewBusy: false }) }
    }
  }
  const restorePreview = (row) => {
    if (owner.sourceContext !== 'native') return
    stopPreview(owner)
    owner.previewRecord = null
    publish({ previewJob: null, previewUnknown: false, previewError: null, previewBusy: false, previewBlocked: false })
    if (row?.state !== 'published') return
    try {
      const raw = localStorage.getItem(previewStorageKey(row))
      if (!raw) return
      const record = JSON.parse(raw)
      if (!samePreviewSource(record?.source, row) || !isSourceUuid(record?.key)) throw new Error()
      if (record.job) record.job = validateNativePreview(record.job, previewExpected(record.source))
      owner.previewRecord = record
      owner.previewUntil = Date.now() + 15 * 60 * 1000
      // Stored job state is only a hint. Display it after authenticated readback.
      publish({ previewUnknown: true })
      void observePreview()
    } catch {
      publish({ previewUnknown: true, previewBlocked: true,
        previewError: 'The saved preview identity is unavailable. Restore its identity before submitting another preview.' })
    }
  }
  const startPreview = async (witness) => {
    if (owner.sourceContext !== 'native' || !current() || owner.previewLocked || owner.locked || owner.exportLocked
      || owner.state.loading || !owner.state.sourceLoaded || owner.state.unknown || owner.state.previewUnknown
      || owner.state.previewBlocked || owner.previewRecord || available()) return
    const row = owner.state.candidate
    if (row?.state !== 'published' || !witness
      || ['source_request_id', 'version', 'receipt_digest'].some((key) => witness[key] !== row[key])) return
    try {
      validateProjectSource(row, owner.projectId, undefined, 'native')
      savePreview({ source: previewWitness(row), key: uuid() })
      owner.previewUntil = Date.now() + 15 * 60 * 1000
      publish({ previewUnknown: true })
      await observePreview()
    } catch {
      publish({ previewError: 'A durable preview identity could not be saved. Preview was not submitted.' })
    }
  }
  const state = view.owner === owner ? view : owner.state
  useEffect(() => {
    if (!state.open || state.previewBusy || state.previewError || state.previewUnknown
      || !['queued', 'running'].includes(state.previewJob?.status)) return
    if (Date.now() >= owner.previewUntil) {
      publish({ previewError: 'Preview observation paused after 15 minutes. Check the existing preview again.' })
      return
    }
    const timer = setTimeout(() => { void observePreview() }, 2000)
    return () => clearTimeout(timer)
  }, [owner, state.open, state.candidate, state.previewJob, state.previewBusy, state.previewError, state.previewUnknown])
  const sourceOperation = sourceBaseCommit(state.candidates, projectId, sourceContext) ? 'edit' : 'create'
  return { ...state, isOpen: state.open, projectId, orgId, sourceContext,
    sourceOperation,
    selectionProblem: sourceSelectionProblem(state.materials, state.selected, projectId, orgId, sourceContext, sourceOperation),
    open, close: () => { clearDownload(); stopPreview(owner); publish({ open: false, previewBusy: false }) }, refresh, prepareDownload,
    startPreview, checkPreview: () => { owner.previewUntil = Date.now() + 15 * 60 * 1000; void observePreview() },
    refreshMaterials: () => { if (!owner.locked) void loadMaterials() },
    setRequirements: (requirements) => { if (!owner.locked) publish({ requirements }) },
    toggleMaterial: (id) => { if (!owner.locked) publish({ selected: owner.state.selected.includes(id)
      ? owner.state.selected.filter((value) => value !== id) : [...owner.state.selected, id], selectionReviewed: true }) },
    reviewSelection: () => publish({ selectionReviewed: true }),
    selectCandidate: (id) => { if (!owner.locked && !owner.state.loading) {
      clearDownload()
      stopPreview(owner)
      owner.previewRecord = null
      const sequence = ++owner.sequence
      publish({ candidate: null, loading: true, sourceLoaded: false, previewJob: null, previewBusy: false, previewUnknown: false, previewError: null })
      void readBack(id, sequence).catch(() => {
        if (sequence === owner.sequence) publish({ error: 'Source readback is unavailable.' })
      }).finally(() => { if (sequence === owner.sequence) publish({ loading: false }) })
    } },
    stage: (options) => mutate('stage', options),
    confirm: (witness) => mutate('confirm', { witness }),
    recover: (witness) => mutate('recover', { witness }),
    reject: (witness) => mutate('reject', { witness }),
  }
}
