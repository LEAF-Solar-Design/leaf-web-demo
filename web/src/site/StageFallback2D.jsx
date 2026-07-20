// Canvas2D fallback stage — a verbatim port of the "Website One Surface" mock's
// drawing code (buildGeo / drawStatic / drawString / drawFrame, incl. the DPR
// handling and the prefers-reduced-motion full-static branch). Rendered by
// StageLayer only when the WebGL Viewer reports glError (or the intake fails
// to load), so the cover never shows a dead void.

import { useEffect, useRef } from 'react'

const W = 1600
const H = 900
const ROUTING = 650 // px/s — the mock's default draw speed
const GAP_T = 0.35
const HOLD_T = 2.6

function buildGeo() {
  const serp = (x, y, w, rows, rH, gap) => {
    const pts = []
    for (let i = 0; i < rows; i++) {
      const cy = y + i * (rH + gap) + rH / 2
      if (i % 2 === 0) pts.push([x, cy], [x + w, cy]); else pts.push([x + w, cy], [x, cy])
    }
    return pts
  }
  const banks = [
    { x: 460, y: 200, w: 220, rows: 10, rH: 14, gap: 9, col: '#ff4d42' },
    { x: 155, y: 480, w: 525, rows: 9, rH: 14, gap: 9, col: '#ff4d42' },
    { x: 775, y: 190, w: 190, rows: 22, rH: 14, gap: 10, col: '#e04fe0' },
    { x: 1055, y: 260, w: 380, rows: 7, rH: 14, gap: 9, col: '#3ae477' },
    { x: 1055, y: 440, w: 380, rows: 7, rH: 14, gap: 9, col: '#e04fe0' },
  ]
  const strings = banks.map((b) => {
    const pts = serp(b.x + 6, b.y, b.w - 12, b.rows, b.rH, b.gap)
    let len = 0; const cum = [0]
    for (let i = 1; i < pts.length; i++) { len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); cum.push(len) }
    return { pts, cum, len, col: b.col }
  })
  return { banks, strings }
}

function drawStatic(g, geo) {
  g.fillStyle = '#0b0d10'; g.fillRect(0, 0, W, H)
  g.fillStyle = 'rgba(255,255,255,.045)'
  for (let gx = 20; gx < W; gx += 24) for (let gy = 20; gy < H; gy += 24) g.fillRect(gx, gy, 1, 1)
  geo.banks.forEach((b) => {
    for (let i = 0; i < b.rows; i++) {
      const ry = b.y + i * (b.rH + b.gap)
      g.strokeStyle = 'rgba(196,202,212,.42)'; g.lineWidth = 0.8
      g.strokeRect(b.x, ry, b.w, b.rH)
      g.strokeStyle = 'rgba(196,202,212,.16)'
      for (let cx = b.x + 26; cx < b.x + b.w; cx += 26) { g.beginPath(); g.moveTo(cx, ry); g.lineTo(cx, ry + b.rH); g.stroke() }
    }
  })
  const poly = (pts, col) => { g.strokeStyle = col; g.lineWidth = 1.5; g.beginPath(); pts.forEach((p, i) => i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])); g.closePath(); g.stroke() }
  poly([[140, 180], [560, 180], [560, 240], [700, 240], [700, 700], [140, 700]], '#35d0e0')
  poly([[270, 250], [340, 250], [340, 320], [410, 320], [410, 390], [340, 390], [340, 460], [270, 460], [270, 390], [200, 390], [200, 320], [270, 320]], '#35d0e0')
  poly([[760, 170], [980, 170], [980, 760], [760, 760]], '#35d977')
  poly([[1040, 240], [1450, 240], [1450, 620], [1040, 620]], '#4a6cf0')
  const lab = (t, x, y, col) => { g.fillStyle = col; g.font = "500 12.5px 'Inter Tight',sans-serif"; g.fillText(t, x, y) }
  lab('Group 18 [207] · −12 / +3', 468, 168, '#ff6a60')
  lab('Group 19 [177] · −12 / +3', 340, 728, '#ff6a60')
  lab('Group 16 [80] · −5 / +10', 762, 152, '#3ae477')
  lab('Group 15 [88] · −13 / +2', 1150, 226, '#7d94ff')
  lab('Group 23 [92] · −2 / +13', 1160, 652, '#e878e8')
  g.strokeStyle = 'rgba(255,255,255,.55)'; g.lineWidth = 1.2
  g.beginPath(); g.moveTo(70, 856); g.lineTo(70, 820); g.moveTo(70, 856); g.lineTo(106, 856); g.stroke()
  g.fillStyle = 'rgba(255,255,255,.55)'; g.font = "10px 'Inter Tight',sans-serif"
  g.fillText('Y', 60, 826); g.fillText('X', 108, 866)
}

function drawString(g, s, dist, head) {
  const { pts, cum } = s
  g.strokeStyle = s.col; g.lineWidth = 1.6
  g.beginPath(); g.moveTo(pts[0][0], pts[0][1])
  let hx = pts[0][0], hy = pts[0][1]
  for (let i = 1; i < pts.length; i++) {
    if (cum[i] <= dist) { g.lineTo(pts[i][0], pts[i][1]); hx = pts[i][0]; hy = pts[i][1] }
    else { const t = (dist - cum[i - 1]) / (cum[i] - cum[i - 1]); hx = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t; hy = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t; g.lineTo(hx, hy); break }
  }
  g.stroke()
  if (head && dist < s.len) { g.fillStyle = '#fff'; g.shadowColor = s.col; g.shadowBlur = 14; g.beginPath(); g.arc(hx, hy, 3.2, 0, 7); g.fill(); g.shadowBlur = 0 }
}

export default function StageFallback2D() {
  const cvRef = useRef(null)

  useEffect(() => {
    const cv = cvRef.current
    if (!cv) return undefined
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    cv.width = W * dpr; cv.height = H * dpr
    const g = cv.getContext('2d'); g.scale(dpr, dpr)
    const geo = buildGeo()
    const st = document.createElement('canvas'); st.width = W * dpr; st.height = H * dpr
    const sg = st.getContext('2d'); sg.scale(dpr, dpr)
    drawStatic(sg, geo)

    if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) {
      g.drawImage(st, 0, 0, W, H)
      geo.strings.forEach((s) => drawString(g, s, s.len, false))
      return undefined
    }

    const t0 = performance.now()
    let raf
    const drawFrame = (t) => {
      const S = geo.strings
      const total = S.reduce((a, s) => a + s.len / ROUTING + GAP_T, 0) + HOLD_T
      const e = ((t - t0) / 1000) % total
      g.drawImage(st, 0, 0, W, H)
      let acc = 0
      for (const s of S) {
        const dur = s.len / ROUTING
        if (e >= acc + dur + GAP_T) { drawString(g, s, s.len, false); acc += dur + GAP_T; continue }
        if (e > acc) drawString(g, s, Math.min((e - acc) * ROUTING, s.len), true)
        break
      }
    }
    const loop = (t) => { drawFrame(t); raf = requestAnimationFrame(loop) }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [])

  return (
    <div className="stage-fallback2d">
      <canvas ref={cvRef} />
    </div>
  )
}
