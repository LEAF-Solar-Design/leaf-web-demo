import { useEffect, useRef, useState } from 'react'
import EscCap from './EscCap.jsx'
import { keyboardTable } from '../lib/actionRegistry.js'
import { readSingleKeyShortcuts, writeSingleKeyShortcuts } from '../lib/singleKeyPreference.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import './shortcutParity.css'

// Shortcut rows and preference membership come from the action registry.
// The modal keeps the resolver anatomy and the shared Escape owner stack.
export default function ShortcutSheet({ open, onClose }) {
  const rootRef = useRef(null)
  const openerRef = useRef(null)
  const close = () => {
    const dialog = rootRef.current
    if (dialog?.open) {
      if (typeof dialog.close === 'function') dialog.close()
      else dialog.removeAttribute('open')
    }
    openerRef.current?.focus()
    onClose?.()
  }
  useEffect(() => {
    if (!open) return
    const dialog = rootRef.current
    const opener = document.activeElement
    openerRef.current = opener
    if (!dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal()
      else dialog.setAttribute('open', '')
    }
    dialog.querySelector('[role="switch"]')?.focus()
    return () => {
      if (dialog.open) {
        if (typeof dialog.close === 'function') dialog.close()
        else dialog.removeAttribute('open')
      }
      opener?.focus()
    }
  }, [open])
  // S25: the single-key switch (WCAG 2.1.4). Readers (the ladder, ResultPanel)
  // read the stored value at keystroke time, so writing it is the whole effect.
  const [singleKeyOn, setSingleKeyOn] = useState(() => readSingleKeyShortcuts())
  useEffect(() => {
    if (open) setSingleKeyOn(readSingleKeyShortcuts())
  }, [open])
  const toggleSingleKey = () => {
    const next = !singleKeyOn
    if (writeSingleKeyShortcuts(next)) setSingleKeyOn(next)
  }

  // S27: Escape belongs to the one owner stack at the sheet layer, so it
  // closes this sheet before a drawer under it and the global ladder never
  // also reads the key. An outside mousedown still dismisses it like every
  // other resolver on this bar.
  useEscapeOwner('shortcuts', open, close, { layer: 'sheet', scope: rootRef })
  useEffect(() => {
    if (!open) return undefined
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) close() }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open, onClose])

  if (!open) return null
  const rows = keyboardTable()
  const groups = [
    { title: 'Shortcuts', singleKey: false },
    { title: 'Single keys', singleKey: true },
  ]

  return (
    <dialog
      className="resolver shortcut-sheet"
      role="dialog"
      aria-label="Keyboard shortcuts"
      ref={rootRef}
      onCancel={(event) => { event.preventDefault(); close() }}
      // This sheet is a shell sibling, not a child of the bar dock. Keep it
      // above the dock and tool rail, and scroll the growing registry inside
      // the panel so every control remains reachable in a short viewport.
      style={{ position: 'fixed', left: 16, right: 16, bottom: 'calc(132px + env(safe-area-inset-bottom, 0px))', margin: '0 auto', maxWidth: 720, maxHeight: 'calc(100dvh - 180px)', overflowY: 'auto', zIndex: 80 }}
    >
      <div className="resolver-header">
        Keyboard shortcuts
        <EscCap type="button" onClick={close} label="Close" />
      </div>
      {groups.map((group) => (
        <section key={group.title} aria-label={group.title}>
          <h2 className="resolver-header">{group.title}</h2>
          {group.singleKey && (
            <button
              type="button"
              className="resolver-row"
              role="switch"
              aria-checked={singleKeyOn}
              aria-label="Single-key shortcuts"
              data-testid="single-key-switch"
              onClick={toggleSingleKey}
            >
              <span className="lbar" aria-hidden="true" />
              <span className="label">Single-key shortcuts</span>
              <span className="key" aria-hidden="true">{singleKeyOn ? 'On' : 'Off'}</span>
            </button>
          )}
          {rows.filter((row) => !!row.singleKey === group.singleKey).map((row) => {
            const off = row.singleKey && !singleKeyOn
            return (
              <div className="resolver-row row" key={row.id} data-testid="shortcut-row" data-shortcut-id={row.id} aria-disabled={off || undefined}>
                <span className="lbar" aria-hidden="true" />
                <span className="label">{row.label}</span>
                <span className="context" data-testid="shortcut-context">{off ? `${row.context} (off)` : row.context}</span>
                <span className="shortcut-keys">
                  {row.kbd.split(/(\+| \/ )/).map((key, index) => index % 2
                    ? <span key={index}>{key}</span>
                    : <kbd className="dp-keycap" key={index}>{key}</kbd>)}
                </span>
              </div>
            )
          })}
        </section>
      ))}
      {/* Honest gap (slice 10b spec): sheets anchors and receipts have no
          index today. Named here rather than rendered as a row nothing
          backs. */}
      <div className="resolver-header">Sheet anchors and receipts: no shortcut index yet</div>
      {/* Slice 13d: the Docs section. Two links, both real files this repo
          ships today (docs/ARCHITECTURE.md, docs/convergence/SURFACE-CONTRACT.md)
          rather than an in-app help route — none exists (grep of routeScene.js
          and SiteRoot.jsx). GitHub blob links off the repo's own origin, so a
          renamed or deleted doc breaks loudly on GitHub's own 404, not silently
          here; DOC_LINKS is the one place either target is spelled. */}
      <div className="resolver-header">Docs</div>
      {DOC_LINKS.map((doc) => (
        <a
          className="resolver-row"
          key={doc.href}
          data-testid="shortcut-doc-link"
          href={doc.href}
          target="_blank"
          rel="noreferrer"
        >
          <span className="lbar" aria-hidden="true" />
          <span className="label">{doc.label}</span>
        </a>
      ))}
    </dialog>
  )
}

const REPO_BLOB = 'https://github.com/LEAF-Solar-Design/leaf-web-demo/blob/main'

export const DOC_LINKS = Object.freeze([
  Object.freeze({ label: 'Architecture overview', href: `${REPO_BLOB}/docs/ARCHITECTURE.md` }),
  Object.freeze({ label: 'Surface contract (the honesty ladder\'s own doc)', href: `${REPO_BLOB}/docs/convergence/SURFACE-CONTRACT.md` }),
])
