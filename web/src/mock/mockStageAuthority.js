let turnSequence = 0

// Local author-preview authority only. The conversation controller loads this
// module exclusively for stage_authority in the shell's mock session mode.
// These ids never enter ensureSession's cache or a live authoring request.
export function startMockStageAuthority(text, classifierHint) {
  if (classifierHint?.purpose !== 'stage_authority') return null
  const session = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return {
    session_id: `demo-session-${session}`,
    turn_id: `demo-turn-${++turnSequence}`,
    status: 'started',
  }
}
