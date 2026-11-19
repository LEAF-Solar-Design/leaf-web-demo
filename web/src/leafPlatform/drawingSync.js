const reasonFor = (error) => error?.reason || error?.message || 'sync_failed'

const planGrammar = (plan) => {
  if (Number.isInteger(plan.plan_grammar) && plan.plan_grammar >= 1 && plan.plan_grammar <= 3) return plan.plan_grammar
  const header = typeof plan.plan_text === 'string' && /^LEAF_MUTATION_PLAN\|([1-3])(?:\r?\n|$)/.exec(plan.plan_text)
  return header ? Number(header[1]) : 1
}

export function resolveDrawingStore(project, ready) {
  const row = Array.isArray(project?.drawing_versions)
    ? project.drawing_versions.find((item) => item?.version_id === ready?.drawingVersionId) : null
  const source = row?.provenance?.source
  if (typeof source?.drawing_id !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,62}$/.test(source.drawing_id) ||
      !Number.isSafeInteger(source.version) || source.version < 1) throw new Error('drawing_store_unmapped')
  return { drawingStoreId: source.drawing_id, boundVersion: source.version }
}

export async function pull({ api, bridge, drawingStoreId, cursor }) {
  const applied = []
  if (cursor == null) return { cursor: null, applied, reason: 'publish_first' }
  try {
    const { head } = await api.getDrawingVersions(false, drawingStoreId)
    if (!Number.isSafeInteger(head) || head < 1) throw new Error('invalid_head_version')
    const last = Math.min(head, cursor + 50)
    const pending = []
    let reason = null
    for (let version = cursor + 1; version <= last; version++) {
      let plan
      try { plan = await api.getDrawingVersionPlan(drawingStoreId, version) }
      catch (error) {
        if (error?.status === 404) { reason = 'no_plan'; break }
        throw error
      }
      if (typeof plan.result_sha256 !== 'string' || plan.result_sha256.length !== 64 || !/^[a-f0-9]{64}$/.test(plan.result_sha256)) {
        reason = 'no_result_digest'
        break
      }
      pending.push({ version, plan })
    }
    const advertisedGrammar = bridge.state?.ready?.capabilities?.planGrammarMax
    const hostGrammar = Number.isInteger(advertisedGrammar) && advertisedGrammar >= 1 ? advertisedGrammar : 2
    const neededGrammar = pending.reduce((max, { plan }) => Math.max(max, planGrammar(plan)), 1)
    if (neededGrammar > hostGrammar) {
      return { cursor, applied, reason: 'plugin_grammar_too_old', neededGrammar, hostGrammar }
    }
    for (const { version, plan } of pending) {
      const outcome = await bridge.applyPlan({
        planText: plan.plan_text, planSha256: plan.plan_sha256,
        parentVersion: version - 1, resultVersion: version, drawingStoreId,
        resultContentSha256: plan.result_sha256,
      })
      if (outcome.status !== 'applied') return { cursor, applied, reason: outcome.reason || outcome.status }
      cursor = version
      applied.push(version)
    }
    return { cursor, applied, reason }
  } catch (error) { return { cursor, applied, reason: reasonFor(error) } }
}

export async function publish({ api, bridge, drawingStoreId, cursor }) {
  try {
    const { head } = await api.getDrawingVersions(false, drawingStoreId)
    if (!Number.isSafeInteger(head) || head < 1) throw new Error('invalid_head_version')
    if (cursor != null && head !== cursor) return { reason: 'pull_first' }
    const checkout = await api.takeCheckout(drawingStoreId)
    if (!checkout.acquired) return { reason: 'checkout_locked' }
    let exported, saved
    try {
      if (!checkout.checkout_capability) throw new Error('checkout_capability_missing')
      exported = await bridge.exportDxf()
      saved = await api.saveEditedDrawingVersion(drawingStoreId, exported.bytes, head, exported.sha256, checkout.checkout_capability)
    } finally { await api.releaseCheckout(drawingStoreId, checkout.checkout_capability) }
    const version = saved?.new_version?.version
    if (!Number.isSafeInteger(version) || version < 1) throw new Error('invalid_saved_version')
    const outcome = await bridge.syncCursor({ drawingStoreId, version, exportSha256: exported.sha256 })
    if (outcome.status !== 'applied') return { reason: outcome.reason || outcome.status }
    return { cursor: version }
  } catch (error) { return { reason: reasonFor(error) } }
}
