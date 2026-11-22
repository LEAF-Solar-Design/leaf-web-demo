// X04 static React report rendering into the existing local JSDOM dependency.
import React from '../web/node_modules/react/index.js'
import ReactDOMServer from '../web/node_modules/react-dom/server.node.js'
import jsdom from '../web/node_modules/jsdom/lib/api.js'
import esbuild from '../web/node_modules/esbuild/lib/main.js'
import { writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
const bundle = await esbuild.build({
  entryPoints: [resolve(root, 'web/src/components/DrawingDependencyReport.jsx')],
  bundle: true, write: false, format: 'esm', platform: 'node', jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
})
const { default: Report } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const results = []
for (const size of [100, 1000, 10000]) {
  const dependencies = Array.from({ length: size }, (_, index) => ({
    kind: 'font', name: `synthetic-${index}.shx`, disposition: 'font remapped', remapped_to: 'sans-serif',
  }))
  let dom
  try {
    const started = performance.now()
    const markup = ReactDOMServer.renderToStaticMarkup(React.createElement(Report, {
      report: { dependencies, coverage_note: 'Synthetic local records only.' },
    }))
    // Default JSDOM does not load subresources or execute scripts.
    dom = new jsdom.JSDOM(markup)
    const rows = [...dom.window.document.querySelectorAll('tbody tr')]
    if (rows.length !== size || rows.some((row, index) => row.cells[1].textContent !== dependencies[index].name
      || row.cells[2].textContent !== 'font remapped to sans-serif')) throw new Error('Report row mismatch')
    results.push({ dependencies: size, visible_dom_rows: rows.length,
      seconds: Number(((performance.now() - started) / 1000).toFixed(6)) })
  } finally {
    dom?.window.close()
  }
}
const encoded = JSON.stringify({ schema: 'leaf.x04.report-render.v1', synthetic: true, timing_gate: false,
  scope: 'Static React markup plus local JSDOM parse and row assertions; no visual-browser proof.', results }, null, 2) + '\n'
const out = process.argv.indexOf('--out')
if (out !== -1) await writeFile(process.argv[out + 1], encoded)
process.stdout.write(encoded)
