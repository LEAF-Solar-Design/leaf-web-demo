// Real Chromium geometry with the shipped cockpit/Solar CSS and synthetic
// drawing/compass obstacles. No app server, account, project or network access.
import { chromium } from '@playwright/test'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const web = fileURLToPath(new URL('../../', import.meta.url))
const css = ['src/site/cockpit.css', 'src/solar/solarFlow.css']
  .map(name => readFileSync(resolve(web, name), 'utf8')).join('\n')
const outIndex = process.argv.indexOf('--out')
const out = outIndex < 0 ? null : resolve(process.argv[outIndex + 1])
const baselineIndex = process.argv.indexOf('--baseline')
const baseline = baselineIndex < 0 ? null : JSON.parse(readFileSync(process.argv[baselineIndex + 1], 'utf8'))
if (out) mkdirSync(out, { recursive: true })
const cases = []
const failures = []
let preservedCases = 0
const intersects = (a, b) => Math.min(a.right, b.right) > Math.max(a.left, b.left)
  && Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top)
const browser = await chromium.launch({ headless: true })
try {
  for (const width of [1024, 1280, 1440, 768, 390]) {
    for (const expanded of [false, true]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } })
      try {
        await page.route('**/*', route => route.abort())
        await page.setContent(`<!doctype html><style>${css}</style><style>
          body { margin:0; font:14px sans-serif; }
          .app { --ck-pane:250px; --cockpit-band-bottom:250px;
            --leaf-layer-base:0; --leaf-radius-control:4px;
            --cv-panel-glass:#202620; --cv-border:#384038; --leaf-ink:#eee;
            --drawer-top:88px; --environment-controls-bottom:190px; }
          #model { position:fixed; left:calc((100vw + 250px)/2 - 86px);
            top:347px; width:172px; height:376px; background:#809480; }
          #compass { position:fixed; right:8px; top:270px; width:112px; height:112px; }
          #objects { position:fixed; left:308px; top:336px; width:360px; height:24px; }
        </style><div class="studio-shell"><div class="app" data-surface="solar">
          <aside class="properties-dock"></aside><div class="workspace-card">
            <div id="model" aria-label="Synthetic drawing"></div>
            <div id="compass" aria-label="Synthetic compass"></div>
            <div id="objects" aria-label="Synthetic Objects disclosure"></div>
            <div class="solar-flow-seat" data-expanded="${expanded}">
              <button class="solar-flow-seat-toggle" aria-expanded="${expanded}">
                Solar flow tools <span aria-hidden="true">${expanded ? '−' : '+'}</span>
              </button>
              <div class="solar-flow-seat-content"><div style="height:240px">Synthetic form</div></div>
            </div>
          </div>
        </div></div>`)
        const rects = await page.evaluate(() => {
          const rect = selector => {
            const box = document.querySelector(selector).getBoundingClientRect()
            return Object.fromEntries(['left', 'right', 'top', 'bottom', 'width', 'height']
              .map(key => [key, Math.round(box[key] * 100) / 100]))
          }
          return { seat: rect('.solar-flow-seat'), model: rect('#model'),
            compass: rect('#compass'), objects: rect('#objects') }
        })
        const row = { width, expanded, ...rects }
        cases.push(row)
        if (baseline && (expanded || width < 1024)) {
          const previous = baseline.cases.find(c => c.width === width && c.expanded === expanded)
          if (!previous || JSON.stringify(previous.seat) !== JSON.stringify(rects.seat)) {
            failures.push(`${width}/${expanded}: expanded or narrow placement changed`)
          } else preservedCases += 1
        }
        if (width >= 1024 && !expanded) {
          for (const obstacle of ['model', 'compass', 'objects']) {
            if (intersects(rects.seat, rects[obstacle])) failures.push(`${width}: collapsed overlaps ${obstacle}`)
          }
          if (rects.seat.left < 0 || rects.seat.right > width || rects.seat.top < 0
              || rects.seat.bottom > 900 || rects.seat.height < 30) failures.push(`${width}: collapsed outside viewport`)
        }
        if (out) await page.screenshot({ path: resolve(out, `${width}-${expanded ? 'open' : 'closed'}.png`) })
      } finally {
        await page.close()
      }
    }
  }
} finally {
  await browser.close()
}
const report = { mode: 'synthetic CSS geometry', cases, failures, preservedCases,
  browserClosed: !browser.isConnected(), passed: failures.length === 0 }
if (out) writeFileSync(resolve(out, 'result.json'), JSON.stringify(report, null, 2) + '\n')
process.stdout.write(JSON.stringify(report) + '\n')
if (failures.length) process.exitCode = 1
