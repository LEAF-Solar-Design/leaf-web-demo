export const ERROR_ACTOR_LABELS = {
  user: 'You',
  workspace_admin: 'Workspace admin',
  approver: 'Approver',
  operator: 'Support',
  service: 'Leaf service',
}

export function errorPresentation(input, fallback = 'Something went wrong.', reasonCode = '') {
  if (!input) {
    return { message: fallback, code: '', nextAction: '', actor: '', retryable: false }
  }
  if (typeof input === 'string') {
    return { message: input, code: '', nextAction: '', actor: '', retryable: false }
  }

  const envelope = input?.body?.error || input?.error || input
  // Keep a specific product cause ahead of the envelope's generic error class.
  // Authoring can still supply its separate reason code explicitly.
  const opaqueReasonCode = [reasonCode, envelope?.reason_code].find(
    (value) => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(value),
  ) || ''
  return {
    message: envelope?.message || input?.message || fallback,
    code: opaqueReasonCode || envelope?.error_code || envelope?.code || input?.errorCode || '',
    nextAction: envelope?.next_action || input?.nextAction || '',
    actor: envelope?.actor || input?.actor || '',
    retryable: Boolean(envelope?.retryable ?? input?.retryable),
  }
}

export function errorActorLabel(actor) {
  return ERROR_ACTOR_LABELS[actor] || actor || ''
}
