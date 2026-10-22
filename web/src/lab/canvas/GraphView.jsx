import Placeholder from '../Placeholder.jsx'

export default function GraphView({ hidden = false }) {
  return (
    <div className="lab-home" hidden={hidden}>
      <Placeholder title="Strategy palette" owner="Palette lands with SEL-102" state="unavailable" />
      <Placeholder title="Dependency canvas" owner="Canvas lands with SEL-102" state="unavailable" />
      <Placeholder title="Tests and Keepers" owner="Tests and Keepers land with SEL-218" state="unavailable" />
    </div>
  )
}
