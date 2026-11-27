// M3 S4: "Open in Studio" from the AutoCAD palette. One click: the account's own workspace,
// a consented copy from AutoCAD (drawing.send_copy), an account upload, a project, an import,
// then Studio opened on that project and drawing. Contract: leaf-research
// plugin-builder-launch-2026-11/announce-plan/M3-DESIGN.md v6 (sections 2, 4, 5, 6, 11).
//
// Tenancy (section 4): the workspace comes only from a fresh /api/session/identity read for
// the current token. The token and workspace captured at the click are rechecked before every
// request; any change stops the flow. The page never sends a tier, owner, role or org body.
// Account uploads are not idempotent, so a lost upload reply is never retried automatically.

import { DEFAULT_WORKSPACE_NAME } from '../controllers/workspace/createWorkspaceController.js'

export const FINGERPRINT_SCHEMA = 'leaf.drawing-fingerprint.v1'
const POLL_LIMIT_MS = 15 * 60_000
const BOOTSTRAP_DETAILS = new Set([
  'verified subject has no active platform identity binding',
  'verified subject has no active platform tenant authority',
])

/** The plugin's SendCopyText.Clean twin: drops control, format and separator characters. */
function clean(value) {
  return String(value ?? '').replace(/[\p{Cc}\p{Cf}\u2028\u2029]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/gu, '')
}

/** The plugin's SendCopyText.FileName twin: base name, cleaned, at most 255 UTF-16 units. */
export function sendCopyFileName(name) {
  let value = String(name ?? '')
  value = clean(value.slice(Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\')) + 1))
  if (value.length > 255) {
    let length = 255
    if (/[\uD800-\uDBFF]/.test(value[length - 1])) length--
    value = value.slice(0, length)
  }
  return !value.trim() || value === '.' || value === '..' ? 'Drawing' : value
}

/** File stem for the project name and the uploaded .dxf name. */
export function fileStem(fileName) {
  const base = sendCopyFileName(fileName)
  const stem = base.replace(/\.[^.]*$/, '').replace(/[. ]+$/, '').slice(0, 116).trim()
  return stem || 'Drawing'
}

/**
 * The one fingerprint line, byte for byte the plugin's DrawingFingerprintV1.Line, so the
 * palette, AutoCAD and Studio print the same text for the same copy.
 */
export function fingerprintLine(fingerprint, fileName) {
  const rows = Array.isArray(fingerprint?.layers) ? fingerprint.layers : []
  let layers = rows.slice(0, 8).map((row) => `${row.name} ${row.count}`).join(', ')
  const more = fingerprint.layer_count - Math.min(8, rows.length)
  if (more > 0) layers += `, and ${more} more layers`
  const objects = fingerprint.model_objects
  return `${sendCopyFileName(fileName)} | sent copy ${String(fingerprint.sha256).slice(0, 8)} | ${layers} | ${objects}${objects === 1 ? ' object' : ' objects'} in model space`
}

/** Section 11 match rule: equal sha256, bytes, model_objects, layer_count, layers_digest, both ok. */
export function fingerprintsMatch(autocad, studio) {
  if (!autocad || !studio || autocad.schema !== FINGERPRINT_SCHEMA || studio.schema !== FINGERPRINT_SCHEMA ||
      autocad.status !== 'ok' || studio.status !== 'ok') return false
  return ['sha256', 'bytes', 'model_objects', 'layer_count', 'layers_digest'].every((key) =>
    autocad[key] !== undefined && autocad[key] !== null && autocad[key] === studio[key])
}

export class SendCopyStop extends Error {
  constructor(code, detail = {}) {
    super(code)
    this.name = 'SendCopyStop'
    this.code = code
    Object.assign(this, detail)
  }
}

const stop = (code, detail) => { throw new SendCopyStop(code, detail) }

/** Plain words for every stop (section 5). `fileName` is the AutoCAD base name when known. */
export function sendCopyMessage(code, { fileName = null, serverMessage = null } = {}) {
  const name = fileName ? sendCopyFileName(fileName) : 'that drawing'
  switch (code) {
    case 'plugin_outdated': return 'Update the Leaf Automation plugin to send this drawing to Studio.'
    case 'uploads_off': return 'Studio is not accepting drawings right now. Nothing was saved. Your DWG was not changed.'
    case 'too_large': return 'This drawing is too large to send (over 16 MB as DXF). Try a smaller copy, for example with WBLOCK.'
    case 'no_model_objects': return 'There is nothing in model space to send.'
    case 'user_declined': return 'Not sent.'
    case 'active_document_changed': return `You switched drawings before it was sent. Go back to ${name} and try again.`
    case 'no_workspace': return 'Your account has no workspace yet. Contact support.'
    case 'plan_lacks_upload': return 'Your plan does not include sending drawings.'
    case 'quota': return serverMessage || 'Your workspace has reached its daily drawing limit. You can send more after the daily reset.'
    case 'lost_reply': return 'We could not confirm the upload. It may be in Studio; check before sending again.'
    case 'read_failed': return `Studio could not read this drawing${serverMessage ? `: ${serverMessage}` : '.'}`
    case 'read_timeout': return 'Studio is still reading this drawing. Check Studio in a few minutes before sending again.'
    case 'import_failed': return 'The copy is in your account but not in a project.'
    case 'project_forbidden': return 'A project with this name already exists in your workspace and you cannot add to it.'
    case 'fingerprint_differs': return "Studio's copy does not match AutoCAD. Send it again."
    case 'fingerprint_unavailable': return 'Studio could not count this copy, so it cannot be checked against AutoCAD. Nothing was opened.'
    case 'account_changed': return 'Your sign-in or workspace changed while sending. Nothing more was sent. Try again.'
    case 'signed_out': return 'Sign in again, then try again.'
    case 'timeout': return 'AutoCAD did not answer. Look for a window in AutoCAD, then try again.'
    case 'session_changed': return 'The AutoCAD connection changed before it was sent. Try again.'
    case 'prompt_pending': return 'AutoCAD is already asking about a copy. Answer it there first.'
    case 'host_refused':
    case 'replay':
    case 'invalid_result':
      return 'AutoCAD did not accept the request. Nothing was sent. Try again.'
    case 'binary_or_unreadable':
    case 'export_failed':
      return 'AutoCAD could not export this drawing. Nothing was sent.'
    default: return 'The drawing could not be sent. Please try again.'
  }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Runs the whole flow. Dependencies are injected so tests drive a fake host and server.
 *   bridge: { sendCopy() }
 *   api: { getSessionIdentity, createOrg, createProject, uploadDrawing, getDrawingUploadStatus,
 *          importUploadedDrawingVersion, setStoredOrgId, getStoredOrgId }
 *   authority(): the current sign-in key (compared, never stored or sent).
 *   rememberProject(orgId, projectId): makes /app resume this project.
 *   onProgress(phase): 'workspace' | 'consent' | 'uploading' | 'reading' | 'project' | 'importing'.
 *   project: { name, requireCreated } to override the default get-or-create by file stem.
 *   sent: a previous `sent` result to reuse (retrying import or project needs no new consent).
 * Resolves { org, workspace, projectId, drawingId, version, fileName, autocad, studio, href }.
 * Throws SendCopyStop with `code` and, when a copy exists, `sent`/`upload` so a retry reuses it.
 */
export async function runSendCopy({ bridge, api, authority, rememberProject, onProgress = () => {},
  project: projectChoice = null, sent: previous = null, upload: previousUpload = null,
  sleep = delay, now = Date.now } = {}) {
  const key = authority()
  if (!key) stop('signed_out')
  let org = null
  const guard = () => {
    if (authority() !== key) stop('account_changed')
    if (org && api.getStoredOrgId() !== org) stop('account_changed')
  }
  const call = async (fn, map) => {
    guard()
    try { return await fn() } catch (error) {
      if (error instanceof SendCopyStop) throw error
      if (error?.status === 401) stop('signed_out')
      const mapped = map?.(error)
      if (mapped) throw mapped
      throw error
    } finally { guard() }
  }

  onProgress('workspace')
  const readIdentity = () => call(() => api.getSessionIdentity(false), (error) =>
    error?.status === 403 && (BOOTSTRAP_DETAILS.has(error?.body?.detail) || BOOTSTRAP_DETAILS.has(error?.body?.error?.message))
      ? new SendCopyStop('bootstrap') : error?.status === 403 ? new SendCopyStop('no_workspace') : null)
  let identity
  try { identity = await readIdentity() } catch (error) {
    if (error?.code !== 'bootstrap') throw error
    await call(() => api.createOrg(DEFAULT_WORKSPACE_NAME), (failure) =>
      failure?.status === 403 || failure?.status === 409 ? new SendCopyStop('no_workspace') : null)
    identity = await readIdentity()
  }
  if (typeof identity?.org !== 'string' || !identity.org) stop('no_workspace')
  org = identity.org
  api.setStoredOrgId(org)
  const workspace = identity.workspace?.name ?? null

  let sent = previous
  if (!sent) {
    onProgress('consent')
    guard()
    const result = await bridge.sendCopy()
    guard()
    if (result.status !== 'sent') stop(result.reason || 'invalid_result')
    sent = result
  }
  const fileName = sent.fileName
  const stem = fileStem(fileName)

  let upload = previousUpload
  if (!upload) {
    onProgress('uploading')
    const file = new File([sent.bytes], `${stem}.dxf`, { type: 'application/dxf' })
    const receipt = await call(() => api.uploadDrawing(file), (error) => {
      if (error?.status === 503) return new SendCopyStop('uploads_off', { sent })
      if (error?.status === 413) return new SendCopyStop('too_large', { sent })
      if (error?.status === 429) return new SendCopyStop('quota', { sent, serverMessage: error.message })
      if (error?.status === 403) return new SendCopyStop('plan_lacks_upload', { sent })
      if (error?.status === 400) return new SendCopyStop('read_failed', { sent, serverMessage: error?.body?.error?.message ?? null })
      return new SendCopyStop('lost_reply', { sent })
    })
    if (typeof receipt?.drawing_id !== 'string' || !receipt.drawing_id) stop('lost_reply', { sent })
    if (receipt.tenant_kind && receipt.tenant_kind !== 'account') stop('account_changed', { sent })
    upload = { drawingId: receipt.drawing_id, version: null, studio: null }
  }

  if (!upload.studio) {
    onProgress('reading')
    const started = now()
    let wait = 1000
    let status
    for (;;) {
      status = await call(() => api.getDrawingUploadStatus(upload.drawingId), () => null).catch((error) => {
        if (error instanceof SendCopyStop) throw error
        return null
      })
      if (status?.status === 'ready') break
      if (status?.status === 'failed') stop('read_failed', { sent, serverMessage: status?.error?.message ?? null })
      if (now() - started >= POLL_LIMIT_MS) stop('read_timeout', { sent, upload })
      await sleep(wait)
      wait = Math.min(5000, Math.round(wait * 1.5))
    }
    const studio = status.fingerprint ?? null
    if (status.content_sha256 === sent.sha256 && (!studio || studio.status !== 'ok')) {
      stop('fingerprint_unavailable', { sent, autocad: sent.fingerprint, studio })
    }
    if (status.content_sha256 !== sent.sha256 || !fingerprintsMatch(sent.fingerprint, studio)) {
      stop('fingerprint_differs', { sent, autocad: sent.fingerprint, studio })
    }
    const version = Number.isSafeInteger(status.extracted_version) && status.extracted_version > 0 ? status.extracted_version : 1
    upload = { ...upload, version, studio }
  }

  onProgress('project')
  const name = projectChoice?.name || stem
  const project = await call(() => api.createProject(name, org, { requireCreated: !!projectChoice?.requireCreated }), (error) =>
    error?.status === 403 ? new SendCopyStop('project_forbidden', { sent, upload })
      : error?.code === 'project_name_collision' ? new SendCopyStop('project_forbidden', { sent, upload })
        : new SendCopyStop('import_failed', { sent, upload }))
  const projectId = project?.project_id ?? project?.id
  if (typeof projectId !== 'string' || !projectId) stop('import_failed', { sent, upload })

  onProgress('importing')
  await call(() => api.importUploadedDrawingVersion(projectId,
    { drawingId: upload.drawingId, version: upload.version, name: sendCopyFileName(fileName).slice(0, 200) },
    { idempotencyKey: `b3:${projectId}:${upload.drawingId}:${upload.version}` }), (error) =>
    error?.status === 403 ? new SendCopyStop('project_forbidden', { sent, upload })
      : new SendCopyStop('import_failed', { sent, upload, projectId }))

  guard()
  // Section 13: /app resumes the remembered project, so remember this one before navigating.
  rememberProject(org, projectId)
  return { org, workspace, projectId, drawingId: upload.drawingId, version: upload.version, fileName,
    autocad: sent.fingerprint, studio: upload.studio, href: `/app?drawing=${encodeURIComponent(upload.drawingId)}` }
}
