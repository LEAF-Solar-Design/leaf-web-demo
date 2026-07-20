// Persistent guided-demo banner (M2). Shown at the top of the center column
// whenever mock is active, so a cold visitor always knows what they are looking
// at. NR ongoing-condition banner: it persists while the condition (mock) is
// true and clears itself when the mode flips — never user-dismissed.
export default function DemoBanner() {
  return (
    <div className="demo-banner enter" role="note" aria-label="Guided demo">
      <div className="demo-banner-text">
        <span className="demo-banner-title">Guided demo — sample rooftop</span>
        <span className="demo-banner-sub">
          Type what you want; Leaf authors a real CAD tool and runs it on this sample rooftop.
        </span>
      </div>
    </div>
  )
}
