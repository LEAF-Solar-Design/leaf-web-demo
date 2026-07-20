import React from 'react'
import ReactDOM from 'react-dom/client'
import SiteRoot from './site/SiteRoot.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <SiteRoot />
    </ErrorBoundary>
  </React.StrictMode>,
)
