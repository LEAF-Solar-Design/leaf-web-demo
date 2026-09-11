import { createHash } from 'node:crypto'
import { captureStagingIdentity } from '../e2e/staging/stagingIdentity.mjs'

const clone = (value) => JSON.parse(JSON.stringify(value))
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const stable = (value) => JSON.stringify(value, function (key, item) {
  return object(item) ? Object.fromEntries(Object.keys(item).sort().map((k) => [k, item[k]])) : item
})
const equal = (a, b) => stable(a) === stable(b)
const digest = (value) => createHash('sha256').update(stable(value)).digest('hex')
const fail = (code) => { throw new ProofFailure(code) }
class ProofFailure extends Error {
  constructor(code) { super(code); this.code = code }
}
const sourceValid = (s) => object(s) && /^[a-f0-9]{64}$/.test(s.sha256)
  && typeof s.authored_at === 'string' && Number.isFinite(Date.parse(s.authored_at))

/** Attach live application-client calls, never credentials. The transport's
 * methods are bound to the existing authenticated shell's api.js instance.
 * No modules are imported from guessed production asset URLs.
 */
export function surfaceConfigProofPage(page, { transport, dedicatedTenantId }) {
  return {
    url: () => page.url(),
    reload: () => page.reload({ waitUntil: 'networkidle' }),
    locator: (selector) => page.locator(selector),
    proof: {
      transport,
      dedicatedTenantId,
      deployment: () => captureStagingIdentity(page.request),
    },
  }
}

// Deliberately narrower than the server schema: preserve declared unrelated
// slots, but only mutate the anchor's boolean cad.authoring shape.
function candidateFor(baseline) {
  const slots = new Set(['chrome', 'toolbar', 'rails', 'commandLine', 'authoring',
    'versions', 'conversations', 'builds', 'contextMenu', 'groundMaterial'])
  if (!object(baseline) || !Object.entries(baseline).every(([id, value]) =>
    ['browser', 'cad', 'solar', 'ios', 'sheets'].includes(id) && object(value)
    && Object.entries(value).every(([key, entry]) => {
      if (!slots.has(key)) return false
      if (key === 'authoring') return typeof entry === 'boolean'
      if (!object(entry)) return false
      if (key === 'chrome') return Object.entries(entry).every(([k, v]) => k === 'tab' && typeof v === 'string')
      if (key === 'conversations') return Object.entries(entry).every(([k, v]) => k === 'scope' && typeof v === 'string')
      if (key === 'builds') return Object.entries(entry).every(([k, v]) => k === 'routes'
        && Array.isArray(v) && v.every((item) => typeof item === 'string'))
      return true
    }))
    || !object(baseline.cad) || baseline.cad.authoring !== true) fail('unsupported_candidate')
  const candidate = clone(baseline)
  candidate.cad.authoring = false
  return candidate
}

/** Returns only allowlisted evidence, including on failure. Never raw errors,
 * overlays, identity bodies, URLs, headers, or user-controlled copy.
 */
export async function runSurfaceConfigProof({ page, baseUrl, expectedSourceSha }) {
  const receipt = { schema: 'leaf.surface-config-live-proof.v1', ok: false,
    mutation: 'not_attempted', restoration: 'not_required', checks: [], current: { readable: false } }
  let baseline, candidate, baselineSource, candidateSource, attempted = false, conflict = false
  let transport, tenant, deploymentBefore
  const code = (error) => error instanceof ProofFailure ? error.code : 'adapter_failure'
  const observe = async () => {
    receipt.current = { readable: false }
    const current = await transport.getSurfaceConfig(false, { fresh: true })
    if (!object(current?.surfaces) || !sourceValid(current.source)) fail('invalid_readback')
    receipt.current = { readable: true, overlayDigest: digest(current.surfaces),
      sourceDigest: digest(current.source), equalsBaseline: equal(current.surfaces, baseline),
      equalsCandidate: equal(current.surfaces, candidate) }
    return current
  }
  const identity = async () => {
    const session = await transport.getSession(false)
    if (!session?.tenant || !session.org || !session.tier) fail('authentication_required')
    if (session.tenant !== tenant) fail('tenant_mismatch')
  }
  const deployment = async () => {
    const d = await page.proof.deployment()
    if (d?.environment !== 'staging' || d.ready !== true || d.degraded_mode !== false
      || d.source_revision !== expectedSourceSha) fail('deployment_mismatch')
    return { source_revision: d.source_revision, environment: d.environment }
  }
  const visible = async (wanted) => {
    const control = page.locator('#workspace-tab-author')
    try { await control.waitFor({ state: wanted ? 'visible' : 'detached', timeout: 15000 }) }
    catch { fail('visible_mismatch') }
    if ((await control.isVisible()) !== wanted) fail('visible_mismatch')
    // A missing control alone must not pass on an error page or another surface.
    const cad = page.locator('#product-surface-tab-cad')
    if (!await cad.isVisible() || await cad.getAttribute('aria-selected') !== 'true') fail('shell_mismatch')
  }
  try {
    const url = new URL(baseUrl)
    const currentUrl = new URL(page.url())
    if (url.origin !== 'https://platform-staging.leafdesign.ai' || url.username || url.password
      || url.search || url.hash || !['', '/'].includes(url.pathname)
      || currentUrl.origin !== url.origin || currentUrl.pathname !== '/try'
      || [...currentUrl.searchParams.keys()].some((key) => key !== 'surface')
      || ![null, 'cad'].includes(currentUrl.searchParams.get('surface'))) fail('target_refused')
    if (!/^[a-f0-9]{40}$/.test(expectedSourceSha)) fail('source_required')
    transport = page.proof?.transport
    tenant = page.proof?.dedicatedTenantId
    // An explicit fixture binding is required, never inferred from a hostname
    // or a tenant name containing "test". The holder supplies this non-secret id.
    if (typeof tenant !== 'string' || !tenant || tenant === 'demo-tenant' || !transport) fail('fixture_required')
    await identity()
    deploymentBefore = await deployment()
    receipt.checks.push('staging_identity_and_source')
    const initial = await observe()
    baseline = clone(initial.surfaces)
    baselineSource = clone(initial.source)
    candidate = candidateFor(baseline)
    await visible(true)
    receipt.checks.push('rendered_control_bound')
    await identity()
    await deployment()
    const prewrite = await observe()
    if (!equal(prewrite.surfaces, baseline) || !equal(prewrite.source, baselineSource)) fail('concurrent_change')
    attempted = true
    receipt.mutation = 'attempted'
    let returned, lost = false
    try { returned = await transport.submitSurfaceConfig(clone(candidate)) }
    catch { lost = true }
    // Always read immediately, including a lost response. Never retry POST.
    const committed = await observe()
    if (!equal(committed.surfaces, candidate)) {
      conflict = !equal(committed.surfaces, baseline) || !equal(committed.source, baselineSource)
      fail(conflict ? 'concurrent_change' : 'readback_mismatch')
    }
    candidateSource = clone(committed.source)
    if (equal(candidateSource, baselineSource)) fail('stale_source_receipt')
    if (!lost && (!sourceValid(returned) || !equal(returned, candidateSource))) fail('source_receipt_mismatch')
    // With a lost response the GET source is the recovered commit receipt.
    receipt.mutation = lost ? 'recovered_by_readback' : 'readback_verified'
    receipt.checks.push('exact_candidate_and_source')
    await page.reload()
    await visible(false)
    receipt.checks.push('visible_change')
    if (!equal(await deployment(), deploymentBefore)) fail('deployment_changed')
    receipt.checks.push('application_source_unchanged')
  } catch (error) {
    receipt.failure = code(error)
    if (receipt.failure === 'concurrent_change') conflict = true
  } finally {
    if (attempted) {
      receipt.restoration = 'failed'
      try {
        await identity()
        const current = await observe()
        let restoredSource = baselineSource
        if (conflict) fail('concurrent_change')
        if (equal(current.surfaces, baseline) && equal(current.source, baselineSource)) {
          // POST did not commit: still prove the original visible state.
        } else {
          if (!candidateSource || !equal(current.surfaces, candidate)
            || !equal(current.source, candidateSource)) fail('concurrent_change')
          let restoredReceipt, lost = false
          try { restoredReceipt = await transport.submitSurfaceConfig(clone(baseline)) }
          catch { lost = true }
          const restored = await observe()
          if (!equal(restored.surfaces, baseline)) fail('restoration_readback_mismatch')
          if (equal(restored.source, candidateSource)) fail('restoration_stale_source')
          if (!lost && (!sourceValid(restoredReceipt) || !equal(restoredReceipt, restored.source))) fail('restoration_receipt_mismatch')
          restoredSource = clone(restored.source)
        }
        await page.reload()
        await visible(true)
        // Re-read after rendering too, so a writer during reload is reported.
        const final = await observe()
        if (!equal(final.surfaces, baseline) || !equal(final.source, restoredSource)) fail('concurrent_change')
        await deployment()
        receipt.restoration = 'verified'
        receipt.checks.push('baseline_get_and_visible_restored')
      } catch (error) {
        receipt.restorationFailure = code(error)
        if (receipt.restorationFailure === 'concurrent_change') receipt.restoration = 'conflict_no_overwrite'
        try { await observe() } catch { receipt.current = { readable: false } }
      }
    }
  }
  receipt.ok = !receipt.failure && receipt.restoration === 'verified'
  return receipt
}
