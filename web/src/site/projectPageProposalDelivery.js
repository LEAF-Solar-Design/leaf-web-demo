// Ephemeral trusted-module connection. There is no replay or pending queue here.
const subscribers = new Set()

export function subscribeProjectPageProposals(subscriber) {
  subscribers.add(subscriber)
  return () => subscribers.delete(subscriber)
}

export function deliverProjectPageProposal(request) {
  for (const subscriber of subscribers) {
    try { if (subscriber(request) === true) return true } catch { /* intake cannot interrupt the transcript */ }
  }
  return false
}
