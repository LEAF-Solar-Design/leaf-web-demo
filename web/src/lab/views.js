import GraphView from './canvas/GraphView.jsx'
import WidgetsView from './widgets/WidgetsView.jsx'
import HistoryView from './history/HistoryView.jsx'
import RunsView from './runs/RunsView.jsx'
import DeploysView from './deploys/DeploysView.jsx'
import TestTradePanel from './testTrade/TestTradePanel.jsx'
import glossary from './contract/sources/leaf-glossary.v1.json'

const label = (id) => glossary.terms.find((term) => term.id === id).plain

export const LAB_VIEWS = Object.freeze([
  { id: 'graph', label: label('strategy'), component: GraphView, path: '/app/lab' },
  { id: 'widgets', label: label('market'), component: WidgetsView, path: '/app/lab/widgets' },
  { id: 'history', label: label('test'), component: HistoryView, path: '/app/lab/history' },
  { id: 'runs', label: label('keeper'), component: RunsView, path: '/app/lab/runs' },
  { id: 'deploys', label: label('lane'), component: DeploysView, path: '/app/lab/deploys' },
  { id: 'test-trade', label: 'Test trade', component: TestTradePanel, path: '/app/lab/test-trade' },
].map(Object.freeze))

let registered = false
let registrations

export function registerLabViews() {
  if (!registered) {
    registrations = LAB_VIEWS
    registered = true
  }
  return registrations
}
