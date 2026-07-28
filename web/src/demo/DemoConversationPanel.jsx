const SUGGESTIONS = [
  'count panels per layer',
  'measure panel area',
  'highlight panels near the edge',
]

export function demoReplyFor(text, decision) {
  const normalized = String(text || '').trim().toLowerCase()
  const confidentTool = decision?.lane === 'run' && decision?.tool && Number(decision.confidence) >= 0.7

  if (confidentTool) {
    return `I matched that to ${decision.tool}. Review the proposed tool below. Nothing runs until you confirm.`
  }
  if (decision?.lane === 'build') {
    return 'I opened Author with your request prefilled. Publishing remains behind review and approval.'
  }
  if (decision?.lane === 'solve') {
    return 'That request needs the cloud solver, which is not connected to this public demo. No work was submitted.'
  }
  if (/^(hi|hello|hey|good\s+(morning|afternoon|evening))\b/.test(normalized)) {
    return 'Hi. You are in the interactive Leaf CAD demo. Try “count panels per layer,” “measure panel area,” or “highlight panels near the edge.”'
  }
  if (/\b(help|what can you do|how does this work)\b/.test(normalized)) {
    return 'I can route requests to the local CAD tools on this sample drawing. Tool runs stay on your device, and I ask you to confirm before anything runs.'
  }
  return 'I received your message. This public preview can route its local CAD tools without sending your text to a cloud assistant. Try one of the suggested CAD requests.'
}

export default function DemoConversationPanel({ turns = [], onSuggestion }) {
  return (
    <div className="converse-card demo-conversation" data-testid="demo-conversation">
      <div className="converse-log" role="log" aria-live="polite" aria-label="Demo conversation">
        {turns.length === 0 && (
          <div className="converse-turn demo-conversation-welcome">
            <div className="converse-msg assistant">
              Send a message or describe a CAD task. This public preview stays local and asks before running a tool.
            </div>
            <div className="demo-conversation-suggestions" aria-label="Suggested CAD messages">
              {SUGGESTIONS.map((suggestion) => (
                <button key={suggestion} type="button" className="chip-neutral" onClick={() => onSuggestion?.(suggestion)}>
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((turn) => (
          <div key={turn.id} className="converse-turn">
            <div className="converse-msg user">{turn.text}</div>
            <div className="converse-msg assistant">{turn.reply}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
