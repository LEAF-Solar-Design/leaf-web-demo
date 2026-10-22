import Placeholder from '../Placeholder.jsx'

export default function DeploysView({ hidden = false }) {
  return (
    <div hidden={hidden}>
      <Placeholder title="Deploys" owner="Keeper releases by Lane are not available yet." state="unavailable" />
    </div>
  )
}
