import Placeholder from '../Placeholder.jsx'

export default function HistoryView({ hidden = false }) {
  return (
    <div hidden={hidden}>
      <Placeholder title="History" owner="Strategy change history is not available yet." state="unavailable" />
    </div>
  )
}
