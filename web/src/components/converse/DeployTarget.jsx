import { useEffect, useState } from 'react'
import { getTenantToolStatus } from '../../api.js'
import { useAccountIdentity } from '../../auth/accountIdentity.js'

export default function DeployTarget({ now = () => Date.now() }) {
  const { email, workspace } = useAccountIdentity()
  const [status, setStatus] = useState(null)
  useEffect(() => {
    let mounted = true
    const read = async () => {
      const result = await getTenantToolStatus()
      if (mounted) setStatus(result)
    }
    void read()
    return () => { mounted = false }
  }, [])

  const target = workspace || (status?.workspaceId ? { id: status.workspaceId, name: status.workspaceName } : null)
  const destination = target
    ? `Deployed to workspace ${target.name ? `${target.name} (${target.id.slice(0, 8)})` : target.id.slice(0, 8)}.`
    : 'Deployed to your workspace.'
  const account = email ? `It arrives in AutoCAD signed in as ${email}.`
    : 'It arrives in AutoCAD signed in with the same account as Studio.'
  const mismatch = status?.workspaceId && workspace?.id
    && status.workspaceId.toLowerCase() !== workspace.id.toLowerCase()
  let activity = null
  if (status && typeof status === 'object') {
    const checkedAt = status.lastPluginCheckAt ? Date.parse(status.lastPluginCheckAt) : NaN
    const elapsed = now() - checkedAt
    if (!Number.isFinite(checkedAt) || elapsed > 24 * 60 * 60 * 1000) {
      activity = <p role="status" className="deploy-target-warning">
        No AutoCAD has checked this workspace in the last day. In AutoCAD, run LEAFSTATUS and check that it shows {email || 'the account you use here'}.
      </p>
    } else {
      const minutes = Math.max(1, Math.floor(elapsed / 60000))
      const count = minutes >= 60 ? Math.floor(minutes / 60) : minutes
      const unit = minutes >= 60 ? 'hour' : 'minute'
      activity = <p>{`AutoCAD last checked this workspace ${count} ${unit}${count === 1 ? '' : 's'} ago.`}</p>
    }
  }

  return <><dt>AutoCAD</dt><dd>
    <span>{destination}</span>{' '}<span>{account}</span>
    {mismatch ? <p role="alert">This card's workspace and the signed-in Studio workspace differ. Reload Studio before deploying again.</p> : activity}
  </dd></>
}
