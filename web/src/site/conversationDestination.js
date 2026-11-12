export function conversationDestination({ hasDockHost, agentMode, sessionId }) {
  return hasDockHost && agentMode && sessionId ? 'dock' : 'rail'
}
