export default function SolarFlowSeat({ seated, children }) {
  if (!seated) return children
  return <div className="solar-flow-seat" data-testid="solar-flow-seat">{children}</div>
}
