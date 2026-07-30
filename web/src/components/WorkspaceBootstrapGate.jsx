import { useState } from 'react'

export default function WorkspaceBootstrapGate({ busy, error, onCreate }) {
  const [name, setName] = useState('My workspace')

  const submit = (event) => {
    event.preventDefault()
    const value = name.trim()
    if (value && !busy) onCreate(value)
  }

  return (
    <section className="session-gate" aria-labelledby="workspace-bootstrap-title">
      <h3 id="workspace-bootstrap-title">Create your Leaf workspace</h3>
      <p>You are signed in. Create your tenant workspace to become its owner, then mount your Claude subscription.</p>
      <form className="proj-create" onSubmit={submit}>
        <label>
          Workspace name
          <input value={name} onChange={(event) => setName(event.target.value)} disabled={busy} />
        </label>
        <button className="btn primary proj-act" type="submit" disabled={busy || !name.trim()}>
          {busy ? 'Creating...' : 'Create workspace'}
        </button>
      </form>
      {error && <div className="field-err" role="alert"><span className="dot red" />{error}</div>}
    </section>
  )
}
