// FIRST import on purpose: importing this module installs the global error +
// unhandledrejection listeners, and ES module evaluation runs in declaration
// order. Anything that throws while the app's own modules evaluate is then
// already covered. Every other importer of telemetry.js (api.js, converse.js,
// ErrorBoundary's dynamic import) is reached only after the app is running,
// and the marketing pages may never reach one at all.
import './telemetry.js'
import React from 'react'
import { useEffect } from 'react'
import SiteRoot from './site/SiteRoot.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import './standard/leaf-standard.css'
import './styles.css'
import { installSurfaceConfigProofBridge } from './proofBridge.js'
import { getSession, getSurfaceConfig, submitSurfaceConfig } from './api.js'

// Next owns the root and development StrictMode. This module is imported
// exclusively by the ssr:false island, after telemetry has been installed.
export default function StudioRoot() {
  useEffect(() => {
    installSurfaceConfigProofBridge(window, {
      hostname: window.location.hostname,
      env: import.meta.env,
      clients: { getSession, getSurfaceConfig, submitSurfaceConfig },
    })
  }, [])
  return (
    <ErrorBoundary>
      <SiteRoot />
    </ErrorBoundary>
  )
}
