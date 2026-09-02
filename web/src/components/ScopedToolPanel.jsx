// The locked single-purpose operator panel (tenant tool scope). Replaces the
// conversational panel for a scoped tenant: one button per scoped tool, run in
// scope order, plus the drawing state. Every run still goes through the
// ordinary slash dispatch (proposal card, confirm, job rail), so the locked
// shell adds no second execution path.
import { scopedTools } from '../site/tenantScope.js'

export default function ScopedToolPanel({ scope, tools, hasDrawing, busy, onRun }) {
  const offered = scopedTools(scope, tools)
  return (
    <div className="tc-scoped" data-testid="scoped-tool-panel">
      <div className="tc-panel-note" role="status">
        <strong>{scope.label}.</strong>{' '}
        {hasDrawing
          ? 'Your drawing is loaded. Run a tool below; the result and downloads appear on the right.'
          : 'Upload a DWG or DXF below to begin.'}
      </div>
      {offered.length === 0 && (
        <div className="tc-operator-empty" data-testid="scoped-tool-empty">
          <span className="dot hollow" />
          <span>No tools are assigned to this workspace yet.</span>
        </div>
      )}
      {offered.map((tool) => (
        <div key={tool.name} className="tc-scoped-tool" data-testid={`scoped-tool-${tool.name}`}>
          <div className="tc-scoped-tool-head">
            <code className="tc-scoped-tool-name">{tool.name}</code>
            <button
              type="button"
              className="tc-bar-chip accent"
              disabled={!hasDrawing || !!busy}
              onClick={() => onRun(tool)}
            >Run</button>
          </div>
          {tool.description && <p className="tc-scoped-tool-desc">{tool.description}</p>}
        </div>
      ))}
    </div>
  )
}
