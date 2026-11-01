'use client'

import dynamic from 'next/dynamic'
import { Component, useMemo, useState } from 'react'
import GroundSkeleton from '../components/GroundSkeleton.jsx'

const createStudioRoot = () => dynamic(() => import('../main.jsx'), {
  ssr: false,
  loading: ({ error }) => {
    if (error) throw error
    return (
      <div style={{ minHeight: '100dvh', background: '#131614', color: '#a3aca3', fontFamily: "'Barlow',system-ui,-apple-system,sans-serif" }}>
        <GroundSkeleton label="workspace" />
      </div>
    )
  },
})

class StudioLoadBoundary extends Component {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    return this.state.failed ? <div role="alert" className="ground-failed">
      <p>The workspace did not load. Your project and session are unchanged.</p>
      <button type="button" className="btn dp-focus" onClick={this.props.retry}>Retry</button>
    </div> : this.props.children
  }
}

export default function StudioClient() {
  const [generation, setGeneration] = useState(0)
  const StudioRoot = useMemo(createStudioRoot, [generation])
  return <StudioLoadBoundary key={generation} retry={() => setGeneration((value) => value + 1)}>
    <StudioRoot />
  </StudioLoadBoundary>
}
