import { PRESETS, RENDERER_VERSION, shapeFor } from './widgetCatalog.js'
export const kashbotWidgetPack = Object.freeze({
  id: 'kashbot', min_renderer: RENDERER_VERSION, source: 'kashbot.board',
  figures: 'leaf-figures.v1', boardRows: 3, ledgerRows: 4, winRateWindow: 'today-utc',
  presets: Object.freeze(Object.entries(PRESETS).map(([id, defaults]) => Object.freeze({ id, kind: shapeFor(id), defaults: Object.freeze({ ...defaults }) }))),
})
export default kashbotWidgetPack
