export const ERROR_ACTOR_LABELS = {
  user: 'You',
  workspace_admin: 'Workspace admin',
  approver: 'Approver',
  operator: 'Support',
  service: 'Leaf service',
}

export function errorPresentation(input, fallback = 'Something went wrong.') {
  if (!input) {
    return { message: fallback, code: '', nextAction: '', actor: '', retryable: false }
  }
  if (typeof input === 'string') {
    return { message: input, code: '', nextAction: '', actor: '', retryable: false }
  }

  const envelope = input?.body?.error || input?.error || input
  return {
    message: envelope?.message || input?.message || fallback,
    code: envelope?.error_code || envelope?.code || input?.errorCode || '',
    nextAction: envelope?.next_action || input?.nextAction || '',
    actor: envelope?.actor || input?.actor || '',
    retryable: Boolean(envelope?.retryable ?? input?.retryable),
  }
}

export function errorActorLabel(actor) {
  return ERROR_ACTOR_LABELS[actor] || actor || ''
}
