// The hero dispatch box (design 20ab544c9f6b): one prompt routed across
// Run / Solve / Build lanes. Ctrl/Cmd+Enter dispatches. Lane hit-indicators
// light as a calm live preview of where the text will route. No animated loader
// — the routing state is a plain word swap on the button.

const LANES = ['run', 'solve', 'build']

export default function PromptBox({ value, onChange, onDispatch, routing, hintLane }) {
  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault()
      onDispatch()
    }
  }
  return (
    <div className={`prompt ${routing ? 'disabled' : ''}`}>
      <div className="field">
        <span className="caret" aria-hidden="true">&gt;</span>
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Describe what Leaf should do — e.g. count panels per layer"
          spellCheck={false}
          disabled={routing}
          aria-label="Dispatch prompt"
        />
      </div>
      <div className="lanes">
        {LANES.map((l) => (
          <span key={l} className={`lane ${hintLane === l ? `hit ${l}` : ''}`}>
            <span className="dot" />{l}
          </span>
        ))}
        <span className="spacer" />
        <kbd>Ctrl</kbd>&nbsp;<kbd>Enter</kbd>&nbsp;
        <button className="dispatch-btn" onClick={onDispatch} disabled={routing || !value.trim()}>
          {routing ? 'Routing' : 'Dispatch'}
        </button>
      </div>
    </div>
  )
}
