// FIRST import on purpose: importing this module installs the global error +
// unhandledrejection listeners, and ES module evaluation runs in declaration
// order. Anything that throws while the app's own modules evaluate is then
// already covered. Every other importer of telemetry.js (api.js, converse.js,
// ErrorBoundary's dynamic import) is reached only after the app is running,
// and the marketing pages may never reach one at all.
import './telemetry.js'
import React from 'react'
import { useEffect, useState } from 'react'
import { bootstrapServiceAuth } from './auth.js'
import ErrorBoundary from './ErrorBoundary.jsx'
import './standard/leaf-standard.css'
import './styles.css'
import './standard/parity-foundation.css'
import { installSurfaceConfigProofBridge } from './proofBridge.js'
import { getSession, getSurfaceConfig, submitSurfaceConfig } from './api.js'
import { canonicalizeDemoEntry } from './site/authBoot.js'

if (typeof window !== 'undefined') canonicalizeDemoEntry()

const siteRootPromise = bootstrapServiceAuth().then(() => import('./site/SiteRoot.jsx'))
  .then(module => ({ Component: module.default }))
  .catch(error => ({ error }))

// Next owns the root and development StrictMode. This module is imported
// exclusively by the ssr:false island, after telemetry has been installed.
export default function StudioRoot() {
  const [SiteRoot, setSiteRoot] = useState(null)
  const [bootError, setBootError] = useState(null)
  useEffect(() => {
    let mounted = true
    siteRootPromise.then(result => {
      if (!mounted) return
      if (result.error) setBootError(result.error)
      else setSiteRoot(() => result.Component)
    })
    return () => { mounted = false }
  }, [])
  useEffect(() => {
    if (!SiteRoot) return
    installSurfaceConfigProofBridge(window, {
      hostname: window.location.hostname,
      env: import.meta.env,
      clients: { getSession, getSurfaceConfig, submitSurfaceConfig },
    })
  }, [SiteRoot])
  return (
    <ErrorBoundary>
      {bootError ? <p role="alert">{bootError.message}</p> : SiteRoot ? <SiteRoot /> : null}
    </ErrorBoundary>
  )
}
