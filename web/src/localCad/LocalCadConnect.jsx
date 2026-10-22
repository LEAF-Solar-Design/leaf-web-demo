import React from 'react'

export default function LocalCadConnect({ status = 'stopped', drawing, summary, onConnect, onDisconnect }) {
  const connect = status === 'stopped' || status === 'unreachable'
  const label = status === 'connected' ? `Connected to AutoCAD · ${drawing ?? ''}`
    : status === 'connecting' ? 'AutoCAD: connecting'
      : status === 'waiting' ? 'AutoCAD: waiting for a drawing' : 'AutoCAD: not found on this computer'
  return <div className="local-cad-connect">
    <button type="button" className="chip-act" data-testid="local-cad-connect"
      onClick={connect ? onConnect : onDisconnect}>{connect ? 'Connect AutoCAD' : 'Disconnect'}</button>
    <div aria-live="polite" data-testid="local-cad-status">{label}</div>
    <div data-testid="local-cad-summary">{summary ?? ''}</div>
  </div>
}
