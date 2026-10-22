import Placeholder from '../Placeholder.jsx'

export default function RunsView({ hidden = false }) {
  return (
    <div hidden={hidden}>
      <Placeholder title="Runs" owner="Test results by Market are not available yet." state="unavailable" />
    </div>
  )
}
