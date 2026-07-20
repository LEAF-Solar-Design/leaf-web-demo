// Persistent guided-demo banner (M2). Shown at the top of the center column
// whenever mock is active, so a cold visitor always knows what they are looking
// at and how to leave. "Exit — explore freely" dismisses the banner and stays
// in free mock exploration — it NEVER drops to the live shell.
export default function DemoBanner({ onExit }) {
  return (
    <div className="demo-banner enter" role="note" aria-label="Guided demo">
      <div className="demo-banner-text">
        <span className="demo-banner-title">Guided demo — sample rooftop</span>
        <span className="demo-banner-sub">
          Type what you want; Leaf authors a real CAD tool and runs it on your drawing.
        </span>
      </div>
      <button type="button" className="demo-banner-exit chip-act" onClick={onExit}>
        Exit — explore freely
      </button>
    </div>
  )
}
