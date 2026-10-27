'use client'

import dynamic from 'next/dynamic'

const StudioRoot = dynamic(() => import('../main.jsx'), {
  ssr: false,
  loading: () => (
    <div style={{ minHeight: '100dvh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#131614', color: '#a3aca3', fontFamily: "'Barlow',system-ui,-apple-system,sans-serif" }}>
      <div style={{ textAlign: 'center' }}>
        <div style={{ width: 18, height: 18, margin: '0 auto 16px', background: '#1e6b45', transform: 'rotate(45deg)', borderRadius: 4 }} />
        <div style={{ fontSize: 14, letterSpacing: '0.01em' }}>Loading workspace…</div>
      </div>
    </div>
  ),
})

export default function StudioClient() {
  return <StudioRoot />
}
