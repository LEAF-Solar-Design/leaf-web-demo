# X05 actual product CSV download replay

Source binding: X05.md, Nov 16 Bensenville append: unknown kW DC is excluded
from PASS but its row remains required. All four count report inputs are required.
Blank DC exports UNREADABLE with blank oracle/delta; invalid nonblank DC blocks
export. This does not change the numeric oracle or grant a discrepancy.

Run the following Python block from the repository root on the Windows desktop
after the focused offhost suite finishes. It uses the installed Python Playwright
and native esbuild, the actual panel and app stylesheet, and only synthetic local
data. It creates a unique artifact directory outside git, launches one Chromium,
and retains its actual CSV, screenshot, bundle and hash-bearing JSON proof there.
It also checks keyboard read/export failure announcements and retries, then
separately reads duplicate panel IDs, foreign string references and missing frame
power, downloads each five-row comparison, and repairs each through a saved reread. No auth,
video, real drawings, environment/secret reads, staging, or uploads are involved.

The same browser then repeats keyboard entry/export at a 390 by 844 viewport,
at 100% and at 200% using explicit local CSS `zoom: 2` on the document element.
This is CSS layout zoom emulation, not native browser zoom or a DPR test; the
computed zoom is asserted and recorded. Maximum accepted count/decimal report
lengths exercise horizontal input scrolling and the panel's keyboard table
scroller. Each replay retains all five CSV rows, normalized DOM parity, focused
field screenshots and hashes. Inspect the retained narrow/zoom images before
acceptance. The final proof records browser/context/server cleanup after it runs.

The stalled-read case waits for the product's actual 20-second timeout (no clock
override). It checks the aborted signal and retry controls, reads distinct retry
evidence with fresh reports, then settles the old promise last. Both actual CSV
downloads must contain only the retry's five rows; timeout and recovery screenshots
and source/artifact hashes remain in the same cleanup-bearing proof.

Finally, the replay switches immutable drawing bindings and matching client
snapshots through normal React props while an old drawing read is deferred.
It records the aborted old request, resolves its distinct revision/totals, and
checks that no old evidence or drafts become accepted. The second drawing needs
an explicit read and fresh reports before its exact five-row CSV can download.
This uses the same mounted panel, with no project-state reset or forced remount.

A same-drawing refresh also reads changed Panel/Inverter/String/DC totals with
fresh reports, then explicitly reads a further revision with valid reassigned
string panel references and equal totals. Revision labels and read counts must
advance; the latter CSV bytes must stay equal. This proves accepted-revision
refresh and aggregate equality, not assignment parity from a count CSV.

A failed-refresh replay first exports successfully, rejects the next explicit
read, and checks that saved evidence disappears and the disabled export produces
no download. A same-mounted-panel retry accepts changed totals and fresh report
lexemes; its actual five-row CSV must match normalized DOM and literal rows.
Error/recovery screenshots and CSV hashes are retained with the cleanup proof.

A valid empty-design replay reads a revision and SHA-bearing response with four
empty collections on the same mounted panel. Zero report totals produce PASS for
Panels, Inverters and Strings. Optimizers remain UNREADABLE without a measured
count, and DC remains UNREADABLE without positive module/panel power evidence;
neither becomes an inferred zero. Its actual five-row CSV, normalized DOM,
literal expected bytes, screenshot and hashes join the same cleanup-bearing proof.

The final report-persistence replay enables a local mocked canonical-file API.
Only the synthetic artifact file stores totals; the panel uses no browser storage.
It saves exact report strings including blank DC, reloads the page and explicitly
reads them back, then simulates another writer. A stale Save must return 409 and
leave the entire canonical file byte-identical until explicit report reload.
The mock is replay infrastructure, not a call to any live project API.

```python
import csv
import copy
import functools
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright, expect

repo = Path.cwd().resolve()
panel = repo / 'web/src/solar/ProjectReconciliationPanel.jsx'
assert panel.is_file(), 'Run from the repository root'
base = Path('D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests')
base.mkdir(parents=True, exist_ok=True)
artifact = Path(tempfile.mkdtemp(prefix='x05-product-download-', dir=base))
binding = dict(kind='project', organization_id='11111111-1111-4111-8111-111111111111',
               project_id='22222222-2222-4222-8222-222222222222',
               drawing_id='33333333-3333-4333-8333-333333333333',
               input_version_id='44444444-4444-4444-8444-444444444444')
def entity(kind, number):
    return f'leaf:{kind}:00000000-0000-4000-8000-{number:012d}'
panels = [entity('panel', 1), entity('panel', 2)]
graph = dict(rev=9, panels=[dict(id=p) for p in panels],
             frames=[dict(id=entity('frame', 1), panel_refs=panels, module_power_watts=400)],
             strings=[dict(id=entity('string', 1), module_count=2, ordered_panel_refs=panels)],
             inverters=[dict(id=entity('inverter', 1), equipment_type='string_inverter'),
                        dict(id=entity('inverter', 2), equipment_type='combiner_box')])
response = dict(ok=True, data=dict(context=binding, intake=dict(
    solar_design_graph=graph, solar_design_graph_sha256='a' * 64)))
next_binding = dict(binding, drawing_id='55555555-5555-4555-8555-555555555555',
                    input_version_id='66666666-6666-4666-8666-666666666666')
next_panels = [entity('panel', n) for n in [11, 12, 13]]
next_graph = dict(rev=42, panels=[dict(id=p) for p in next_panels],
                  frames=[dict(id=entity('frame', 11), panel_refs=next_panels, module_power_watts=500)],
                  strings=[dict(id=entity('string', 11), module_count=2, ordered_panel_refs=next_panels[:2]),
                           dict(id=entity('string', 12), module_count=1, ordered_panel_refs=next_panels[2:])],
                  inverters=[dict(id=entity('inverter', n), equipment_type='central_inverter') for n in [11, 12]] +
                            [dict(id=entity('inverter', 13), equipment_type='combiner_box')])
next_response = dict(ok=True, data=dict(context=next_binding, intake=dict(
    solar_design_graph=next_graph, solar_design_graph_sha256='b' * 64)))
entry = artifact / 'entry.jsx'
entry.write_text(
    'import React from ' + json.dumps((repo / 'web/node_modules/react/index.js').as_posix()) + ';\n'
    'import {createRoot} from ' + json.dumps((repo / 'web/node_modules/react-dom/client.js').as_posix()) + ';\n'
    'import Panel from ' + json.dumps(panel.as_posix()) + ';\n'
    'const binding = Object.freeze(' + json.dumps(binding) + ');\n'
    'const nextBinding = Object.freeze(' + json.dumps(next_binding) + ');\n'
    'const nextResponse = ' + json.dumps(next_response) + ';\n'
    'const response = ' + json.dumps(response) + ';\n' + '''
let attempts = 0;
let deferOld = false, settleOld, oldSignal;
let rejectNext = false;
window.rejectNextRead = () => { rejectNext = true; };
window.scopeProof = {newReads: 0, oldSettled: false};
window.deferOldRead = () => { deferOld = true; };
window.readAttempts = () => attempts;
window.resolveOldRead = () => { settleOld(); window.scopeProof.oldSettled = true; };
window.setSavedGraph = graph => { response.data.intake.solar_design_graph = structuredClone(graph); };
const client = {
  getSnapshot: () => ({binding, authorityKey: 'synthetic'}),
  subscribe: () => () => {},
  readContext: async ({signal}) => {
    if (++attempts === 1) throw new Error('synthetic private read error');
    if (rejectNext) { rejectNext = false; throw new Error('synthetic private refresh error'); }
    if (deferOld) {
      deferOld = false;
      oldSignal = signal;
      const captured = structuredClone(response);
      return new Promise(resolve => { settleOld = () => resolve(captured); });
    }
    return response;
  },
};
const nextClient = {
  getSnapshot: () => ({binding: nextBinding, authorityKey: 'synthetic-next'}),
  subscribe: () => () => {},
  readContext: async () => { window.scopeProof.newReads++; return nextResponse; },
};
const root = createRoot(document.getElementById('root'));
const isCurrent = () => true;
const render = (solarBinding, authorityKey, projectClient) => root.render(
  <Panel solarBinding={solarBinding} authorityKey={authorityKey}
    projectClient={projectClient} isCurrent={isCurrent} />);
window.switchDrawing = () => render(nextBinding, 'synthetic-next', nextClient);
window.oldReadAborted = () => oldSignal?.aborted === true;
window.enableReportPersistence = () => {
  let loseSaveResponse = false;
  let failReportGet = false;
  window.loseNextSaveResponse = () => { loseSaveResponse = true; };
  window.failNextReportReload = () => { failReportGet = true; };
  const exchange = async (method, body, signal) => {
    if (method === 'GET' && failReportGet) { failReportGet = false; throw new Error('Synthetic report GET failure'); }
    const result = await fetch('/mock-engineering-totals', {
      method, signal, headers: {'Content-Type': 'application/json'},
      ...(body ? {body: JSON.stringify(body)} : {}),
    });
    return {ok: result.ok, status: result.status, data: await result.json()};
  };
  const reportClient = {...client, readContext: async () => structuredClone(response),
    loadEngineeringTotals: ({signal}) => exchange('GET', null, signal),
    saveEngineeringTotals: async ({signal, ...body}) => {
      const result = await exchange('POST', body, signal);
      if (loseSaveResponse) { loseSaveResponse = false; throw new Error('Synthetic lost save response'); }
      return result;
    },
  };
  render(binding, 'synthetic', reportClient);
};
render(binding, 'synthetic', client);
''', encoding='utf-8')
command = [str(repo / 'web/node_modules/@esbuild/win32-x64/esbuild.exe'), str(entry),
           '--bundle', '--format=esm', '--jsx=automatic',
           '--alias:react=' + (repo / 'web/node_modules/react').as_posix(),
           '--alias:react-dom=' + (repo / 'web/node_modules/react-dom').as_posix(),
           '--outfile=' + str(artifact / 'app.js'),
           '--metafile=' + str(artifact / 'meta.json')]
result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=90)
assert result.returncode == 0, 'Local source bundling failed'
shutil.copyfile(repo / 'web/src/styles.css', artifact / 'styles.css')
if (repo / 'web/public/fonts').is_dir():
    shutil.copytree(repo / 'web/public/fonts', artifact / 'fonts')
(artifact / 'index.html').write_text('''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><title>X05 synthetic product download</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/styles.css"></head><body>
<main id="root"></main><script type="module" src="/app.js"></script></body></html>''', encoding='utf-8')

canonical_file = artifact / 'mock-canonical-project.json'
canonical_file.write_text(json.dumps(dict(intake=response['data']['intake'], version=7,
    record=None, revision=0, receipts={})), encoding='utf-8')
canonical_lock = threading.Lock()
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def report_reply(self, status, document):
        payload = json.dumps(document).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def do_GET(self):
        if self.path != '/mock-engineering-totals':
            return super().do_GET()
        with canonical_lock:
            document = json.loads(canonical_file.read_text(encoding='utf-8'))
            self.report_reply(200, dict(record=document['record'], revision=document['revision'], canWrite=True))
    def do_POST(self):
        assert self.path == '/mock-engineering-totals'
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with canonical_lock:
            document = json.loads(canonical_file.read_text(encoding='utf-8'))
            key = request['idempotencyKey']
            receipt = document['receipts'].get(key)
            if receipt:
                assert receipt['request'] == request
                return self.report_reply(200, receipt['response'])
            if request['expectedRevision'] != document['revision']:
                return self.report_reply(409, {})
            assert request['graphSha256'] == 'a' * 64
            assert len(request['drafts']) == 5 and all(isinstance(v, str) for v in request['drafts'])
            document['record'] = dict(schema='leaf.engineering-reconciliation.v1', binding={k: v for k, v in binding.items() if k != 'kind'},
                graph_sha256=request['graphSha256'], values=request['drafts'],
                provenance='user_supplied_unverified')
            document['revision'] += 1
            result = dict(record=document['record'], revision=document['revision'], canWrite=True)
            document['receipts'][key] = dict(request=request, response=result)
            canonical_file.write_text(json.dumps(document), encoding='utf-8')
            self.report_reply(200, result)

server = thread = browser = context = playwright = None
thread_started = False
proof_path = artifact / 'proof.json'
try:
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(artifact)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thread_started = True
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(accept_downloads=True, viewport={'width': 1440, 'height': 900})
    origin = f'http://127.0.0.1:{server.server_port}'
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
    page = context.new_page()
    page.goto(origin, wait_until='networkidle')
    read = page.get_by_role('button', name='Read saved design', exact=True)
    page.keyboard.press('Tab')
    expect(read).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.get_by_role('alert')).to_contain_text('Try reading it again')
    expect(read).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.get_by_text('Saved design revision 9.', exact=True)).to_be_visible()
    labels = ['Panels', 'Optimizers', 'Inverters', 'Strings', 'kW DC']
    def tab_to(target):
        for _ in range(20):
            page.keyboard.press('Tab')
            if target.evaluate('(node) => node === document.activeElement'):
                expect(target).to_be_focused()
                return
        raise AssertionError('Control was not reachable by keyboard')
    for label, value in zip(labels, ['2', '1', '2', '1', '0.8']):
        field = page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True)
        tab_to(field)
        page.keyboard.insert_text(value)
    page.keyboard.press('Tab')
    download_button = page.get_by_role('button', name='Download CSV', exact=True)
    expect(download_button).to_be_focused()
    page.evaluate('''() => {
      const original = URL.createObjectURL.bind(URL);
      URL.createObjectURL = (...args) => {
        URL.createObjectURL = original;
        throw new Error('synthetic private download error');
      };
    }''')
    page.keyboard.press('Enter')
    expect(page.get_by_role('alert')).to_contain_text('Try Download CSV again')
    expect(download_button).to_be_focused()
    with page.expect_download() as pending:
        page.keyboard.press('Enter')
    download = pending.value
    assert download.failure() is None
    csv_path = artifact / 'engineering-reconciliation.csv'
    download.save_as(csv_path)
    expect(page.get_by_role('alert')).to_have_count(0)
    # Also exercise the explicit pointer click on the same product download.
    with page.expect_download() as clicked:
        download_button.click()
    clicked.value.save_as(artifact / 'engineering-reconciliation-click.csv')
    assert clicked.value.failure() is None
    assert csv_path.read_bytes() == (artifact / 'engineering-reconciliation-click.csv').read_bytes()
    metrics = dict(zip([label.casefold() for label in labels], ['panels', 'optimizers', 'inverters', 'strings', 'kw_dc']))
    def visible_rows():
        visible = []
        rows = page.locator('tbody tr')
        assert rows.count() == 5
        for row in rows.all():
            cells = row.locator('td')
            studio, delta = cells.nth(0).inner_text().strip(), cells.nth(2).inner_text().strip()
            verdict = cells.nth(3).inner_text().strip().split(':', 1)[0].strip()
            visible.append(dict(metric=metrics[row.locator('th').inner_text().strip().casefold()],
                                studio='' if studio == 'Unavailable' else studio,
                                oracle=cells.nth(1).locator('input').input_value(),
                                delta='' if delta == 'Unavailable' else delta,
                                verdict='UNREADABLE' if verdict == 'Unavailable' else verdict))
        return visible
    visible = visible_rows()
    with csv_path.open(newline='', encoding='utf-8') as stream:
        actual = list(csv.DictReader(stream))
    assert actual == visible, 'Actual browser download differs from the rendered comparison'
    assert actual[1] == dict(metric='optimizers', studio='', oracle='1', delta='', verdict='UNREADABLE')
    assert actual[2] == dict(metric='inverters', studio='1', oracle='2', delta='-1', verdict='FAIL')
    assert [row['verdict'] for row in actual] == ['PASS', 'UNREADABLE', 'FAIL', 'PASS', 'PASS']
    page.screenshot(path=str(artifact / 'comparison.png'), full_page=True)
    def capture_comparison(name):
        rendered = visible_rows()
        with page.expect_download() as pending:
            download_button.click()
        download = pending.value
        assert download.failure() is None
        path = artifact / (name + '.csv')
        download.save_as(path)
        with path.open(newline='', encoding='utf-8') as stream:
            downloaded = list(csv.DictReader(stream))
        assert len(downloaded) == 5 and downloaded == rendered, name + ': CSV/DOM mismatch'
        page.screenshot(path=str(artifact / (name + '.png')), full_page=True)
        return dict(visible=rendered, downloaded=downloaded, csv=path.name)

    cases = []
    for index, (name, affected) in enumerate([
        ('duplicate-panel-ids', {0, 3, 4}),
        ('foreign-string-reference', {3}),
        ('incomplete-frame-power', {4}),
    ]):
        malformed = copy.deepcopy(graph)
        malformed['rev'] = 10 + index * 2
        if name == 'duplicate-panel-ids':
            malformed['panels'].append(copy.deepcopy(malformed['panels'][0]))
        elif name == 'foreign-string-reference':
            malformed['strings'][0]['ordered_panel_refs'] = [panels[0], entity('panel', 99)]
        else:
            del malformed['frames'][0]['module_power_watts']
        page.evaluate('(graph) => window.setSavedGraph(graph)', malformed)
        assert visible_rows() == actual, 'Client mutation changed accepted evidence without a read'
        read.click()
        expect(page.get_by_text(f"Saved design revision {malformed['rev']}.", exact=True)).to_be_visible()
        broken = capture_comparison(name)
        for row_index, row in enumerate(broken['downloaded']):
            if row_index in affected:
                assert row == dict(metric=actual[row_index]['metric'], studio='',
                                   oracle=actual[row_index]['oracle'], delta='', verdict='UNREADABLE')
            else:
                assert row == actual[row_index], name + ': valid sibling changed'
        repaired = copy.deepcopy(graph)
        repaired['rev'] = malformed['rev'] + 1
        page.evaluate('(graph) => window.setSavedGraph(graph)', repaired)
        assert visible_rows() == broken['visible'], 'Repair was accepted without a saved reread'
        read.click()
        expect(page.get_by_text(f"Saved design revision {repaired['rev']}.", exact=True)).to_be_visible()
        recovery = capture_comparison(name + '-repaired')
        assert recovery['downloaded'] == actual, name + ': repaired comparison did not recover'
        assert (artifact / recovery['csv']).read_bytes() == csv_path.read_bytes()
        cases.append(dict(name=name, affected_metrics=[actual[i]['metric'] for i in sorted(affected)],
                          malformed=broken, repaired=recovery, explicit_saved_reread=True))

    narrow_replays = []
    maximum_reports = ['9007199254740991'] * 4 + ['999999999999999999.999999999999999999']
    for zoom in [1, 2]:
        name = f'narrow-css-zoom-{zoom}'
        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate('(zoom) => { document.documentElement.style.zoom = String(zoom); }', zoom)
        computed_zoom = page.evaluate('Number(getComputedStyle(document.documentElement).zoom)')
        assert computed_zoom == zoom
        tab_to(read)
        page.keyboard.press('Enter')
        expect(page.get_by_text(f"Saved design revision {repaired['rev']}.", exact=True)).to_be_visible()
        field_proofs = []
        for index, (label, value) in enumerate(zip(labels, maximum_reports)):
            field = page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True)
            tab_to(field)
            page.keyboard.press('Control+A')
            page.keyboard.insert_text(value)
            expect(field).to_have_value(value)
            expect(field).to_have_attribute('aria-invalid', 'false')
            # Tab focus must reveal the whole control inside every clipping ancestor.
            geometry = field.evaluate('''node => {
              const r = node.getBoundingClientRect();
              let left = 0, right = innerWidth, top = 0, bottom = innerHeight;
              for (let p = node.parentElement; p; p = p.parentElement) {
                const s = getComputedStyle(p), b = p.getBoundingClientRect();
                if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) {
                  left = Math.max(left, b.left); right = Math.min(right, b.right);
                }
                if (/(auto|scroll|hidden|clip)/.test(s.overflowY)) {
                  top = Math.max(top, b.top); bottom = Math.min(bottom, b.bottom);
                }
              }
              return {left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                revealed: r.left >= left - 1 && r.right <= right + 1 &&
                  r.top >= top - 1 && r.bottom <= bottom + 1};
            }''')
            assert geometry['revealed'], (name, label, geometry)
            page.keyboard.press('Home')
            assert field.evaluate('(node) => node.selectionStart') == 0
            page.screenshot(path=str(artifact / f'{name}-field-{index}-start.png'))
            page.keyboard.press('End')
            assert field.evaluate('(node) => node.selectionStart') == len(value)
            page.screenshot(path=str(artifact / f'{name}-field-{index}-end.png'))
            field_proofs.append(dict(label=label, value=value, keyboard_reached=True, geometry=geometry))
        tab_to(download_button)
        expect(download_button).to_be_in_viewport()
        rendered = visible_rows()
        with page.expect_download() as pending:
            page.keyboard.press('Enter')
        download = pending.value
        assert download.failure() is None
        path = artifact / (name + '.csv')
        download.save_as(path)
        with path.open(newline='', encoding='utf-8') as stream:
            downloaded = list(csv.DictReader(stream))
        assert len(downloaded) == 5 and downloaded == rendered
        assert [row['oracle'] for row in downloaded] == maximum_reports
        assert [row['studio'] for row in downloaded] == ['2', '', '1', '1', '0.8']
        assert [row['delta'] for row in downloaded] == [
            '-9007199254740989', '', '-9007199254740990', '-9007199254740990',
            '-999999999999999999.199999999999999999']
        assert [row['verdict'] for row in downloaded] == ['FAIL', 'UNREADABLE', 'FAIL', 'FAIL', 'FAIL']
        page.screenshot(path=str(artifact / (name + '.png')), full_page=True)
        scroller = page.get_by_role('region', name='Reconciliation comparison table', exact=True)
        tab_to(scroller)
        for _ in range(60):
            page.keyboard.press('ArrowLeft')
        page.screenshot(path=str(artifact / (name + '-table-left.png')))
        for _ in range(60):
            page.keyboard.press('ArrowRight')
        scroll = scroller.evaluate('(node) => ({left: node.scrollLeft, width: node.clientWidth, content: node.scrollWidth})')
        assert scroll['content'] <= scroll['width'] + 1 or scroll['left'] > 0
        page.screenshot(path=str(artifact / (name + '-table-right.png')))
        overflow = page.evaluate('''() => ({viewport: innerWidth,
          body: document.body.getBoundingClientRect().width,
          bodyScroll: document.body.scrollWidth, bodyClient: document.body.clientWidth,
          pageScroll: document.scrollingElement.scrollWidth, pageClient: document.scrollingElement.clientWidth})''')
        assert overflow['bodyScroll'] <= overflow['bodyClient'] + 1, overflow
        assert overflow['pageScroll'] <= overflow['pageClient'] + 1, overflow
        narrow_replays.append(dict(name=name, viewport=dict(width=390, height=844),
                                   zoom_method='document.documentElement.style.zoom (CSS layout emulation)',
                                   computed_zoom=computed_zoom, fields=field_proofs, scroll=scroll,
                                   overflow=overflow, visible=rendered, downloaded=downloaded, csv=path.name,
                                   keyboard_download=True))
    # Same mounted drawing/client: only an explicit read accepts a new revision.
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.evaluate('document.documentElement.style.zoom = "1"')
    refresh_before = visible_rows()
    refresh_reads = page.evaluate('window.readAttempts()')
    changed_revision = copy.deepcopy(next_graph)
    changed_revision['rev'] = 70
    page.evaluate('(graph) => window.setSavedGraph(graph)', changed_revision)
    assert visible_rows() == refresh_before
    assert page.evaluate('window.readAttempts()') == refresh_reads
    read.click()
    expect(page.get_by_text('Saved design revision 70.', exact=True)).to_be_visible()
    assert page.evaluate('window.readAttempts()') == refresh_reads + 1
    fresh_reports = ['003', '00', '02', '02', '1.500']
    for label, value in zip(labels, fresh_reports):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    refreshed = capture_comparison('same-drawing-changed-revision')
    expected_refresh = [dict(zip(['metric', 'studio', 'oracle', 'delta', 'verdict'], row)) for row in [
        ['panels', '3', '003', '0', 'PASS'],
        ['optimizers', '', '00', '', 'UNREADABLE'],
        ['inverters', '2', '02', '0', 'PASS'],
        ['strings', '2', '02', '0', 'PASS'],
        ['kw_dc', '1.5', '1.500', '0', 'PASS'],
    ]]
    assert refreshed['downloaded'] == expected_refresh
    assert [row['studio'] for row in refresh_before] == ['2', '', '1', '1', '0.8']
    reassigned_revision = copy.deepcopy(changed_revision)
    reassigned_revision['rev'] = 71
    reassigned_revision['strings'][0]['ordered_panel_refs'] = [next_panels[0], next_panels[2]]
    reassigned_revision['strings'][1]['ordered_panel_refs'] = [next_panels[1]]
    # Independent fixture validity: positive counts, unique ownership, complete panel set.
    refs = [ref for string in reassigned_revision['strings'] for ref in string['ordered_panel_refs']]
    assert len(refs) == len(set(refs)) == 3 and set(refs) == set(next_panels)
    assert all(string['module_count'] == len(string['ordered_panel_refs']) > 0
               for string in reassigned_revision['strings'])
    assert changed_revision['strings'] != reassigned_revision['strings']
    page.evaluate('(graph) => window.setSavedGraph(graph)', reassigned_revision)
    expect(page.get_by_text('Saved design revision 70.', exact=True)).to_be_visible()
    assert visible_rows() == refreshed['visible']
    assert page.evaluate('window.readAttempts()') == refresh_reads + 1
    read.click()
    expect(page.get_by_text('Saved design revision 71.', exact=True)).to_be_visible()
    expect(page.get_by_text('Saved design revision 70.', exact=True)).to_have_count(0)
    assert page.evaluate('window.readAttempts()') == refresh_reads + 2
    reassigned = capture_comparison('same-drawing-equal-totals-reassignment')
    assert reassigned['downloaded'] == expected_refresh
    assert (artifact / refreshed['csv']).read_bytes() == (artifact / reassigned['csv']).read_bytes()
    revision_refresh = dict(binding=binding, changed_revision=changed_revision,
                            reassigned_revision=reassigned_revision, fresh_reports=fresh_reports,
                            reads_before=refresh_reads, reads_after=refresh_reads + 2,
                            before=refresh_before, changed=refreshed, reassigned=reassigned,
                            equal_csv_bytes=True, assignment_parity_claimed=False)

    # A valid empty saved graph is evidence for zero counts, not for measured DC
    # or optimizer totals. Keep the current mounted drawing and explicitly read.
    empty_graph = dict(rev=72, panels=[], frames=[], strings=[], inverters=[])
    empty_reports = ['0', '0', '0', '0', '0']
    empty_reads_before = page.evaluate('window.readAttempts()')
    page.evaluate('(graph) => window.setSavedGraph(graph)', empty_graph)
    assert visible_rows() == reassigned['visible']
    read.click()
    expect(page.get_by_text('Saved design revision 72.', exact=True)).to_be_visible()
    assert page.evaluate('window.readAttempts()') == empty_reads_before + 1
    for label, value in zip(labels, empty_reports):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    expect(download_button).to_be_enabled()
    expect(page.get_by_text('The saved graph has no explicit measured optimizer count.', exact=False)).to_be_visible()
    expect(page.get_by_text('DC needs complete, unique frame coverage and positive supported module watts for every panel.',
                            exact=False)).to_be_visible()
    empty = capture_comparison('valid-empty-design')
    expected_empty = [dict(zip(['metric', 'studio', 'oracle', 'delta', 'verdict'], row)) for row in [
        ['panels', '0', '0', '0', 'PASS'],
        ['optimizers', '', '0', '', 'UNREADABLE'],
        ['inverters', '0', '0', '0', 'PASS'],
        ['strings', '0', '0', '0', 'PASS'],
        ['kw_dc', '', '0', '', 'UNREADABLE'],
    ]]
    assert empty['downloaded'] == empty['visible'] == expected_empty
    empty_bytes = (artifact / empty['csv']).read_bytes()
    assert empty_bytes == (
        'metric,studio,oracle,delta,verdict\r\n'
        '"panels","0","0","0","PASS"\r\n'
        '"optimizers","","0","","UNREADABLE"\r\n'
        '"inverters","0","0","0","PASS"\r\n'
        '"strings","0","0","0","PASS"\r\n'
        '"kw_dc","","0","","UNREADABLE"\r\n').encode('ascii')
    empty_design = dict(binding=binding, graph=empty_graph,
                        response_sha256=response['data']['intake']['solar_design_graph_sha256'],
                        reports=empty_reports, comparison=empty, expected=expected_empty,
                        csv_sha256=hashlib.sha256(empty_bytes).hexdigest(),
                        screenshot='valid-empty-design.png',
                        screenshot_sha256=hashlib.sha256((artifact / 'valid-empty-design.png').read_bytes()).hexdigest(),
                        explicit_saved_reread=True, no_remount=True,
                        optimizer_measurement_available=False, positive_panel_power_available=False)

    # Successful export -> rejected refresh -> changed totals, same mounted scope.
    baseline_reports = ['02', '00', '01', '01', '0.800']
    page.evaluate('(graph) => window.setSavedGraph(graph)', graph)
    read.click()
    expect(page.get_by_text('Saved design revision 9.', exact=True)).to_be_visible()
    for label, value in zip(labels, baseline_reports):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    refresh_baseline = capture_comparison('failed-refresh-baseline')
    assert [row['studio'] for row in refresh_baseline['downloaded']] == ['2', '', '1', '1', '0.8']
    failure_downloads = []
    def observe_failed_refresh(download):
        failure_downloads.append(download.suggested_filename)
    page.on('download', observe_failed_refresh)
    failure_reads = page.evaluate('window.readAttempts()')
    page.evaluate('window.rejectNextRead()')
    read.click()
    expect(page.get_by_role('alert')).to_have_text('Saved design could not be read. Try reading it again.')
    expect(page.get_by_text('Saved design revision 9.', exact=True)).to_have_count(0)
    expect(read).to_be_enabled()
    expect(download_button).to_be_disabled()
    failed_rows = visible_rows()
    assert all(row['studio'] == row['delta'] == '' and row['verdict'] == 'UNREADABLE' for row in failed_rows)
    assert [row['oracle'] for row in failed_rows] == baseline_reports
    # Native click on a disabled button must not emit an actual browser download.
    download_button.evaluate('(node) => node.click()')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert failure_downloads == []
    assert page.evaluate('window.readAttempts()') == failure_reads + 1
    page.screenshot(path=str(artifact / 'failed-refresh-error.png'), full_page=True)
    recovered_graph = copy.deepcopy(next_graph)
    recovered_graph['rev'] = 75
    recovery_reports = ['003', '00', '02', '02', '1.500']
    for label, value in zip(labels, recovery_reports):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    expect(download_button).to_be_disabled()
    page.evaluate('(graph) => window.setSavedGraph(graph)', recovered_graph)
    read.click()
    expect(page.get_by_text('Saved design revision 75.', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    expect(download_button).to_be_enabled()
    assert failure_downloads == []
    recovered = capture_comparison('failed-refresh-recovered')
    assert recovered['downloaded'] == expected_refresh
    assert page.evaluate('window.readAttempts()') == failure_reads + 2
    assert failure_downloads == ['engineering-reconciliation.csv']
    page.remove_listener('download', observe_failed_refresh)
    failed_refresh = dict(binding=binding, baseline=refresh_baseline, failed=failed_rows,
                          disabled_export_checked=True, downloads_during_failure=0,
                          read_attempts=2, recovery_reports=recovery_reports, recovery=recovered,
                          baseline_sha256=hashlib.sha256((artifact / refresh_baseline['csv']).read_bytes()).hexdigest(),
                          recovery_sha256=hashlib.sha256((artifact / recovered['csv']).read_bytes()).hexdigest(),
                          assignment_parity_claimed=False)
    assert failed_refresh['baseline_sha256'] != failed_refresh['recovery_sha256']

    # Real product timeout: no timer interception or virtual elapsed-time claim.
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.evaluate('document.documentElement.style.zoom = "1"')
    timeout_old = copy.deepcopy(graph)
    timeout_old['rev'] = 81
    timeout_old['frames'][0]['module_power_watts'] = 700
    timeout_retry = copy.deepcopy(next_graph)
    timeout_retry['rev'] = 82
    timeout_old_drafts = ['02', '01', '01', '01', '1.400']
    for label, value in zip(labels, timeout_old_drafts):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    attempts_before = page.evaluate('window.readAttempts()')
    page.evaluate('(graph) => { window.setSavedGraph(graph); window.deferOldRead(); }', timeout_old)
    read.click()
    expect(page.get_by_text('Reading saved design.', exact=True)).to_be_visible()
    expect(read).to_be_disabled()
    expect(download_button).to_be_disabled()
    assert not page.evaluate('window.oldReadAborted()')
    expect(page.get_by_role('alert')).to_have_text(
        'Saved design read timed out. Try reading it again.', timeout=30000)
    assert page.evaluate('window.oldReadAborted()')
    expect(read).to_be_enabled()
    expect(download_button).to_be_disabled()
    expect(page.get_by_text('Saved design revision 81.', exact=True)).to_have_count(0)
    for label, value in zip(labels, timeout_old_drafts):
        field = page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True)
        expect(field).to_be_enabled()
        expect(field).to_have_value(value)
    page.screenshot(path=str(artifact / 'stalled-read-timeout.png'), full_page=True)
    retry_drafts = ['003', '00', '02', '02', '1.500']
    for label, value in zip(labels, retry_drafts):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    page.evaluate('(graph) => window.setSavedGraph(graph)', timeout_retry)
    read.click()
    expect(page.get_by_text('Saved design revision 82.', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    retry_comparison = capture_comparison('timeout-retry')
    expected_retry = [dict(zip(['metric', 'studio', 'oracle', 'delta', 'verdict'], row)) for row in [
        ['panels', '3', '003', '0', 'PASS'],
        ['optimizers', '', '00', '', 'UNREADABLE'],
        ['inverters', '2', '02', '0', 'PASS'],
        ['strings', '2', '02', '0', 'PASS'],
        ['kw_dc', '1.5', '1.500', '0', 'PASS'],
    ]]
    assert retry_comparison['downloaded'] == expected_retry
    page.evaluate('window.resolveOldRead()')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    expect(page.get_by_text('Saved design revision 81.', exact=True)).to_have_count(0)
    expect(page.get_by_text('Saved design revision 82.', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    expect(read).to_be_enabled()
    assert page.evaluate('window.oldReadAborted()')
    assert page.evaluate('window.readAttempts()') == attempts_before + 2
    late_comparison = capture_comparison('timeout-old-settled-last')
    assert late_comparison['downloaded'] == expected_retry
    assert (artifact / retry_comparison['csv']).read_bytes() == (artifact / late_comparison['csv']).read_bytes()
    timeout_recovery = dict(timeout_method='actual product 20000ms timer; no clock override',
                            old_revision=81, retry_revision=82, old_request_aborted=True,
                            old_settled_last=True, read_attempts=2, old_drafts=timeout_old_drafts,
                            retry_drafts=retry_drafts, retry=retry_comparison, late=late_comparison)

    # Change props/client scope on the same mounted panel during an old read.
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.evaluate('document.documentElement.style.zoom = "1"')
    old_deferred = copy.deepcopy(graph)
    old_deferred['rev'] = 91
    old_deferred['frames'][0]['module_power_watts'] = 700
    old_drafts = ['02', '01', '02', '01', '0.800']
    for label, value in zip(labels, old_drafts):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    page.evaluate('(graph) => { window.setSavedGraph(graph); window.deferOldRead(); }', old_deferred)
    downloads_after_switch = []
    page.on('download', lambda download: downloads_after_switch.append(download.suggested_filename))
    read.click()
    expect(page.get_by_text('Reading saved design.', exact=True)).to_be_visible()
    expect(download_button).to_be_disabled()
    page.evaluate('window.switchDrawing()')
    expect(read).to_be_enabled()
    page.wait_for_function('window.oldReadAborted()')
    for label in labels:
        expect(page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True)).to_have_value('')
    expect(download_button).to_be_disabled()
    assert page.evaluate('window.scopeProof.newReads') == 0
    page.evaluate('window.resolveOldRead()')
    # Let the old promise continuation and any React work finish before checking isolation.
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    expect(page.get_by_text('Saved design revision 91.', exact=True)).to_have_count(0)
    expect(page.get_by_text('Saved design revision 42.', exact=True)).to_have_count(0)
    before_new_read = visible_rows()
    assert all(row['studio'] == row['oracle'] == row['delta'] == '' and row['verdict'] == 'UNREADABLE'
               for row in before_new_read)
    assert page.evaluate('window.scopeProof.newReads') == 0
    assert downloads_after_switch == []
    page.screenshot(path=str(artifact / 'drawing-switch-old-settled.png'), full_page=True)
    read.click()
    expect(page.get_by_text('Saved design revision 42.', exact=True)).to_be_visible()
    expect(download_button).to_be_disabled()
    fresh_without_reports = visible_rows()
    assert [row['studio'] for row in fresh_without_reports] == ['3', '', '2', '2', '1.5']
    assert all(row['oracle'] == row['delta'] == '' and row['verdict'] == 'UNREADABLE'
               for row in fresh_without_reports)
    assert downloads_after_switch == []
    page.screenshot(path=str(artifact / 'drawing-switch-fresh-read.png'), full_page=True)
    new_drafts = ['003', '00', '02', '02', '1.500']
    for index, (label, value) in enumerate(zip(labels, new_drafts)):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
        if index < 3:
            expect(download_button).to_be_disabled()
    expect(download_button).to_be_enabled()
    isolated = capture_comparison('drawing-switch-new-only')
    expected_new = [dict(zip(['metric', 'studio', 'oracle', 'delta', 'verdict'], row)) for row in [
        ['panels', '3', '003', '0', 'PASS'],
        ['optimizers', '', '00', '', 'UNREADABLE'],
        ['inverters', '2', '02', '0', 'PASS'],
        ['strings', '2', '02', '0', 'PASS'],
        ['kw_dc', '1.5', '1.500', '0', 'PASS'],
    ]]
    assert isolated['downloaded'] == expected_new
    assert downloads_after_switch == ['engineering-reconciliation.csv']
    assert page.evaluate('window.scopeProof') == dict(newReads=1, oldSettled=True)
    isolation = dict(old_binding=binding, new_binding=next_binding, old_revision=91, new_revision=42,
                     old_request_aborted=page.evaluate('window.oldReadAborted()'),
                     old_response_settled=True, before_new_read=before_new_read,
                     fresh_without_reports=fresh_without_reports, old_drafts=old_drafts,
                     new_drafts=new_drafts, new_read_count=1, downloads=downloads_after_switch,
                     comparison=isolated)
    # Return to the original synthetic scope in forced colors, with a file-backed mock API.
    page.emulate_media(forced_colors='active')
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    page.reload(wait_until='networkidle')
    page.evaluate('window.enableReportPersistence()')
    read.click()
    expect(page.get_by_text('No saved engineering totals. Entered drafts were retained.', exact=True)).to_be_visible()
    persisted_values = ['002', '01', '02', '01', '']
    for label, value in zip(labels, persisted_values):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    save = page.get_by_role('button', name='Save engineering totals', exact=True)
    save.click()
    expect(page.get_by_text('Engineering totals saved.', exact=True)).to_be_visible()
    first_document = json.loads(canonical_file.read_text(encoding='utf-8'))
    assert first_document['record']['values'] == persisted_values
    assert first_document['intake'] == response['data']['intake'] and first_document['version'] == 7
    page.reload(wait_until='networkidle')
    page.evaluate('window.enableReportPersistence()')
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)') == ['', '', '', '', '']
    read.click()
    expect(page.get_by_text('Saved engineering totals loaded.', exact=True)).to_be_visible()
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)') == persisted_values
    restored = capture_comparison('canonical-report-restored-blank-dc')
    assert restored['downloaded'][-1]['oracle'] == restored['downloaded'][-1]['delta'] == ''
    assert restored['downloaded'][-1]['verdict'] == 'UNREADABLE'
    newer_values = ['003', '01', '02', '01', '0.8']
    external = context.request.post(origin + '/mock-engineering-totals', data=dict(
        graphSha256='a' * 64, drafts=newer_values, expectedRevision=1,
        idempotencyKey='synthetic-other-writer'))
    assert external.status == 200
    newer_bytes = canonical_file.read_bytes()
    page.get_by_role('textbox', name='Panels engineering report/BOM', exact=True).fill('004')
    save.click()
    expect(page.get_by_text('Engineering totals changed. Reload saved engineering totals before saving.', exact=True)).to_be_visible()
    expect(save).to_be_disabled()
    assert canonical_file.read_bytes() == newer_bytes
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)')[0] == '004'
    page.screenshot(path=str(artifact / 'canonical-stale-save-rejected.png'), full_page=True)
    page.get_by_role('button', name='Reload saved engineering totals', exact=True).click()
    expect(page.get_by_text('Saved engineering totals loaded.', exact=True)).to_be_visible()
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)') == newer_values
    assert canonical_file.read_bytes() == newer_bytes
    # Commit succeeds in the mock file store but its response is lost.
    lost_values = ['005', '01', '02', '01', '0.8']
    for label, value in zip(labels, lost_values):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    page.evaluate('window.loseNextSaveResponse()')
    save.click()
    expect(page.get_by_text('Engineering totals could not be saved. Retry Save engineering totals.', exact=True)).to_be_visible()
    committed_bytes = canonical_file.read_bytes()
    committed_document = json.loads(committed_bytes)
    assert committed_document['record']['values'] == lost_values
    assert committed_document['revision'] == 3
    page.get_by_role('button', name='Reload saved engineering totals', exact=True).click()
    expect(page.get_by_text('Saved engineering totals loaded.', exact=True)).to_be_visible()
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)') == lost_values
    assert canonical_file.read_bytes() == committed_bytes
    recovered = capture_comparison('lost-save-response-recovered')
    # A failed report GET must retain a usable dirty comparison and export.
    dirty_values = ['006', '01', '02', '01', '0.8']
    for label, value in zip(labels, dirty_values):
        page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True).fill(value)
    page.evaluate('window.failNextReportReload()')
    page.get_by_role('button', name='Reload saved engineering totals', exact=True).click()
    expect(page.get_by_text('Engineering totals could not be loaded. Reload saved engineering totals to try again.', exact=True)).to_be_visible()
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)') == dirty_values
    assert canonical_file.read_bytes() == committed_bytes
    failed_reload = capture_comparison('failed-report-get-dirty-csv')
    page.get_by_role('button', name='Reload saved engineering totals', exact=True).click()
    expect(page.get_by_text('Saved engineering totals loaded.', exact=True)).to_be_visible()
    assert page.get_by_role('textbox').evaluate_all('(nodes) => nodes.map(n => n.value)') == lost_values
    assert canonical_file.read_bytes() == committed_bytes
    reload_recovered = capture_comparison('failed-report-get-recovered-csv')
    final_document = json.loads(committed_bytes)
    assert final_document['intake'] == first_document['intake'] and final_document['version'] == 7
    report_persistence = dict(restored=restored, exact_values=persisted_values, blank_dc=True,
        forced_colors_active=True, textual_verdicts_and_five_csv_rows=True,
        stale_save_rejected=True, stale_save_file_bytes_unchanged=True,
        explicit_reload_values=newer_values, graph_and_version_unchanged=True,
        lost_save_response_recovered=True, recovery_without_write=True, recovered=recovered,
        failed_reload=failed_reload, failed_reload_recovered=reload_recovered,
        canonical_file_sha256=hashlib.sha256(committed_bytes).hexdigest())
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    inputs = json.loads((artifact / 'meta.json').read_text(encoding='utf-8'))['inputs']
    sources = {(repo / name).resolve() for name in inputs}
    sources.add(repo / 'web/src/styles.css')
    sources.add(repo / 'tools/xbench/tests/X05-product-download-repro.md')
    proof = dict(synthetic=True, rows_matched=5, visible=visible, downloaded=actual,
                 malformed_graph_replays=cases,
                 narrow_zoom_replays=narrow_replays,
                 two_drawing_isolation=isolation,
                 same_drawing_revision_refresh=revision_refresh,
                 valid_empty_design=empty_design,
                 failed_refresh_export_isolation=failed_refresh,
                 stalled_read_timeout_recovery=timeout_recovery,
                 canonical_report_persistence=report_persistence,
                 keyboard_read_retry=True, keyboard_export_retry=True,
                 source_sha256={str(path): digest(path) for path in sorted(sources)},
                 artifact_sha256={path.name: digest(path) for path in artifact.iterdir() if path.is_file()})
    proof_path.write_text(json.dumps(proof, indent=2), encoding='utf-8')
finally:
    try:
        if context is not None:
            context.close()
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            try:
                if playwright is not None:
                    playwright.stop()
            finally:
                try:
                    if server is not None and thread_started:
                        server.shutdown()
                finally:
                    if server is not None:
                        server.server_close()
                    if thread_started:
                        thread.join(timeout=10)
                        assert not thread.is_alive(), 'Local server did not stop'
if proof_path.is_file():
    proof = json.loads(proof_path.read_text(encoding='utf-8'))
    proof['cleanup'] = dict(context_closed=True, browser_closed=True, playwright_stopped=True,
                            server_closed=True, server_thread_stopped=not thread.is_alive())
    proof_path.write_text(json.dumps(proof, indent=2), encoding='utf-8')
print(json.dumps({'proof': str(proof_path), 'rows_matched': 5, 'keyboard_retries_passed': 2,
                  'malformed_graph_repairs': len(cases), 'narrow_zoom_replays': len(narrow_replays),
                  'two_drawing_isolation': True, 'stalled_read_timeout_recovery': True,
                  'valid_empty_design': True,
                  'same_drawing_revision_refresh': True}))
```

## Independent two-panel CAS replay

Parent: X05 durable report reconciliation. The executed support fixture is
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-report-persistence-hhl93ep9/replay.py`.
It bundles the actual panel and persistence client from the lifecycle worktree.
It runs one browser, two independently scoped clients, one synthetic file API
and one file store. Its saved-design read is mocked. It does not prove PostgreSQL
concurrency or staging behavior. No authentication, customer data or video is used.

Use the compatible React/esbuild cache and installed Python Playwright. Run that
retained `replay.py` on Windows. The viewport is 1920 by 2200, forced colors active;
both complete panels and their controls fit side by side. The helper records exact
source hashes and closes browser, context, server and thread in `finally`.

1. Explicitly Read saved design in both panels. Enter A's reports
   `001,0,01,1,blank` and B's `002,0,01,1,0.4`, both based on file revision zero.
2. Save A and wait for its saved cue. Save B and require the conflict cue. B keeps
   its entered values, A remains unchanged, and the entire newer file stays equal.
3. Download A and B CSVs. Normalize each panel's five DOM rows independently and
   require exact equality with its downloaded rows, including unreadable text.
4. Explicitly reload B's report. It adopts A's saved values without a write.
   Change B's Panels to `003`, DC to `0.4`, and Save. Change A's Panels to `004`
   and Save from its stale baseline. Require conflict and unchanged newer bytes.
5. Download each panel again and compare all rows. Explicitly reload A, require
   B's saved values, and repeat both downloads. Six exports and 30 rows must agree.
6. Set A's Panels to `005`, hold only its next Save response, and Save. Edit A to
   `006` while pending. Explicitly Read saved design. The old save observation is
   cancelled, the fresh read detects the committed newer report, and A retains
   `006` with explicit reload required. Deliver the old receipt last. It must not
   show a saved cue or change the file, drafts, baseline or B's accepted values.

Stop on extra writes, row disagreement, stale saved cue, changed newer file or an
uncaught browser error. `proof.json`, `independent-clients-conflict.png` and
`read-cancels-save-observation.png` preserve the result. This replay is separate
from the single-client lost-response recipe above; its two clients share neither
the client instance nor panel state.

## Late Reload after a newer retry

Parent: X05 durable report reconciliation. Executed replay and source hashes:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-report-persistence-_isz7cma/replay.py`.
Run this retained Python helper with the same React/esbuild cache and Playwright
setup described above. It uses actual panels and independent persistence clients,
a synthetic file API, and mocked saved-design reads. This is offline browser
evidence. The 1920 by 2200 forced-colors viewport fits both complete panels.

1. Complete the preceding competing-save and failed-GET recovery sequence. After
   the held Save response is discarded, explicitly Reload both panels. Each
   adopts Panels `005` at file revision 3.
2. Hold A's next GET response after the server has captured that record. Click
   Reload in A. In B, enter Panels `007` and Save. Require the saved cue and file
   revision 4.
3. Wait for A's real 20-second load timeout and retry message. Explicitly Reload
   A again. Require the loaded cue and `007`. Deliver the old GET response last.
   A must retain `007`, and B must remain unchanged.
4. Download both actual CSVs and compare every field with each panel's five DOM
   rows. Enter Panels `008` in A and Save. The captured PUT must name
   `expected_revision: 4`, the file must advance to revision 5, and B stays `007`.
   Download A's CSV again and compare all five rows.

The complete run checks 11 exports against 55 DOM rows. `proof.json` records the
timeout, traffic, exact source hashes and cleanup. `late-reload-newer-retry.png`
shows both panels after the stale response. Stop on any stale draft, wrong CAS
revision, row mismatch or browser error. The helper closes its browser, context,
server and thread in `finally`. No video, staging or PostgreSQL claim is made.

## Shared-client teardown replay

Parent: X05 report persistence. Retained executable support fixture:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-shared-client-_b4l0h8b/replay.py`.
Run with the same local React/esbuild and Python Playwright setup above. Two
actual panels use one actual persistence client, one synthetic file API and
mocked saved-design reads. The 1920 by 2200 forced-colors viewport fits the
surviving panel and all five rows. This is offline evidence, without video.

1. Read saved design in both panels. Require two subscriptions. Enter A's
   Panels `001` and B's `002`, with `0,01,1,0.4` in their other fields.
2. Hold A's next Reload GET response after capture. Start that Reload. Hold
   B's next Save response, then Save B from revision zero.
3. Unmount only A. Require A's signal aborted, B's Save signal still active
   and one remaining subscription. Deliver A's old GET. B must keep its dirty
   `002` and its pending Save cue.
4. Deliver B's Save response. Require the saved cue. Reload B explicitly and
   download its CSV. Require all five rows to equal the visible table.
5. Change B's Panels to `003`, Save, Reload and download again. Captured PUTs
   must have expected revisions `[0,1]`. Both exports equal ten DOM rows.
6. Capture `shared-client-survivor.png`, then unmount B. Require zero
   subscriptions. The helper closes context, browser, server and thread in
   `finally`; `proof.json` records cleanup, source hashes and exact traffic.

Stop on another panel's abort, stale drafts, changed CAS baseline, retained
subscription, export disagreement or uncaught error. No staging or database
claim is made. The separate focused tests also cover losing shared-client
Save drafts, per-field receipt substitution and edit/revert during Save.

## Same-scope client replacement replay

Parent: X05 report receipt isolation. Executed support fixture:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-client-replacement-qodfmvug/replay.py`.
Use the local React/esbuild and Python Playwright setup above. Two actual panels
use independent clients; A then receives a replacement client at the same drawing
and authority. The shared file API and saved-design reads are mocked. The
1920 by 2200 forced-colors viewport fits both panels and all five rows.

1. Read saved design in both panels. Enter A's Panels `001` and Save, holding
   its response after file revision 1 commits. Other fields are `0,01,1,0.4`.
2. Reload B, change its Panels to `003`, then Save at expected revision 1.
   Require its saved cue and file revision 2.
3. Replace only A's client, leaving the drawing and authority unchanged.
   Require Save disabled and the explicit Read prompt. Read saved design again.
   Require the loaded report cue and `003` from revision 2.
4. Deliver the old client's Save receipt last. A must keep `003` with no saved
   cue from that receipt. Download both actual CSVs and compare all five rows
   with each visible table. Capture `same-scope-client-replaced.png`.
5. Enter A's Panels `004`, Save and export again. Require expected revision 2,
   file revision 3, unchanged B values and exact CSV/DOM rows. Three exports
   must equal 15 rows. `proof.json` binds source hashes, traffic and cleanup.

Stop on stale drafts, a false saved cue, an incorrect CAS baseline or a row
mismatch. Browser, context, server and thread close in `finally`. The retained
replay and `REPLAY.md` are offline proof, without database or staging acceptance.

## Late-subscriber replay

Parent: X05 shared-client report isolation. Executed support fixture:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-late-subscriber-rvof4393/replay.py`.
Use the local React/esbuild and Python Playwright setup above. The two actual
panels share one actual persistence client and a synthetic file API. Saved-design
reads are mocked. The 1920 by 2200 forced-colors viewport fits both full panels.

1. Mount only A and Read saved design. Enter `001,0,01,1,0.4`, Save, and hold
   the response after the file commits revision 1. Edit A's Panels to `009`.
2. Mount B on the same client. Require blank inputs, a Read prompt and disabled
   Save. Mounting must issue no report GET. Read saved design explicitly in B.
   Require loaded values `001,0,01,1,0.4` from revision 1.
3. Deliver A's held receipt. Require its submitted-values-saved cue with newer
   edits unsaved. A keeps `009`; B keeps `001` without inheriting A's dirty state.
4. Download both actual CSVs and compare every cell with each panel's five DOM
   rows. Capture `late-subscriber-independent.png`.
5. Change B's Panels to `003` and Save. Require expected revision 1 and stored
   revision 2. A stays `009`. Export B again and compare all five rows. Three
   exports equal 15 rows, including the ten rows from the paired download.
6. Unmount both panels and require zero subscriptions. Browser, context, server
   and thread close in `finally`. `proof.json` records source hashes, traffic,
   observer counts and cleanup; the executable replay and `REPLAY.md` are retained.

Stop on an implicit GET, inherited draft, wrong CAS revision, row mismatch or
uncaught error. This is offline browser evidence, without staging or PostgreSQL
acceptance. No video or private engineering inputs are used.

## Panel-local accessible-name and help replay

Parent: X05 multi-panel reconciliation. Executed support fixture:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-label-isolation-d7cpi9g5/replay.py`.
Run with the same local React/esbuild and Python Playwright setup above. Two
actual panels share one actual persistence client. Saved-design reads and the
file API are mocked. The 1920 by 2200 forced-colors viewport fits both panels.

1. Read saved design in both panels. Fill A with `001,0,01,1,0.4` and B with
   `002,0,01,1,0.400`. Change A's Panels and B's kW DC to `bad`.
2. Require both fields marked invalid and both CSV controls disabled. Resolve
   each input by its accessible name within its panel. Require every input ID
   unique in the document and every described help ID to resolve inside that
   same panel. Capture `panel-local-invalid-help.png`.
3. Repair only A's Panels to `001`. Require A's CSV enabled, while B's invalid
   DC text and disabled CSV remain unchanged. Download A's actual CSV and
   compare all five rows with its DOM table.
4. Repair B's DC to `0.400`. Download B's actual CSV and compare all five rows.
   Two exports must equal ten DOM rows, preserving each report's exact lexemes.
   Capture `panel-local-repaired-csv.png`. No PUT may occur in this replay.
5. Unmount both panels and require zero subscriptions. Browser, context, server
   and thread close in `finally`; `proof.json` records source hashes, assertions,
   traffic and cleanup. The executable and settled `REPLAY.md` remain beside it.

Stop on duplicate IDs, a cross-panel help reference, an enabled export for an
invalid report, a sibling repair or a CSV mismatch. This is offline evidence,
without a staging or PostgreSQL claim. No video or customer data is used.

## Busy persistence recovery replay

Parent: X05 report request lifecycle. Executed support fixture:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-busy-persistence-uppn73z8/replay.py`.
Use the local React/esbuild and Python Playwright setup above. Two actual panels
share one actual client, with a synthetic file API and mocked saved-design read.
The 1920 by 2200 forced-colors viewport fits both full panels.

1. Read saved design in both panels. Enter A's `001,0,01,1,0.4`. Save and hold
   the response after the file commits revision 1. Set A busy while waiting.
   Require all four actions and all five inputs disabled.
2. Deliver the Save receipt while A remains busy. Require the saved cue and
   unchanged values, with actions still disabled. Capture
   `busy-save-actions-disabled.png`. Clear busy and export A's actual CSV;
   compare all five rows with its DOM table.
3. Reload B, change Panels to `003` and Save at expected revision 1. Hold A's
   next explicit Reload response, set A busy, and deliver that response.
   Require loaded values `003,0,01,1,0.4`, a loaded cue and disabled actions.
   Capture `busy-reload-actions-disabled.png`.
4. Clear busy and export A again. Enter Panels `004` and Save. Require expected
   revision 2 and file revision 3. Export once more. Three CSVs equal 15 DOM rows.
   Capture `busy-recovered-csv.png`.
5. Unmount both panels, require zero observers, and close browser, context,
   server and thread in `finally`. `proof.json` binds source hashes, request
   traffic, accepted baselines and cleanup. Replay and `REPLAY.md` are retained.

Stop on an enabled action while busy, a lost draft, a wrong CAS revision or a
CSV mismatch. Focused synthetic tests separately cover rejection and timeout.
This browser replay proves successful held responses only, without staging or
PostgreSQL acceptance. No video or private engineering inputs are used.

## Save and Reload error-redaction replay

Parent: X05 report recovery. Executed support fixture:
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-error-redaction-qfm7itt1/replay.py`.
Use the local React/esbuild and Python Playwright setup above. Actual panels
share one actual persistence client, with a synthetic file API and mocked
saved-design reads. Transport exceptions contain only a synthetic sentinel,
HTML and control characters. No real secret is used. The 1920 by 2200
forced-colors viewport fits both full panels.

1. Read saved design in both panels. Enter A's `001,0,01,1,0.4`. Reject its
   next Save transport with the injected error. Require fixed, polite live
   feedback, unchanged drafts, no saved cue, no image node and no sentinel in
   the DOM. The synthetic file must remain absent. Export the five retained
   rows and capture `save-error-sanitized.png`.
2. Retry Save explicitly. Require the saved cue and file revision 1. Change
   Panels to `002`, then reject the next explicit Reload transport similarly.
   Require fixed live feedback, retained `002`, unchanged file revision 1,
   no injected node or sentinel, and an exact five-row CSV. Capture
   `reload-error-sanitized.png`.
3. Reload explicitly without injection. Require restored `001` and export
   again. Three actual CSVs must equal 15 normalized DOM rows. Enter `003`
   and Save: require expected revision 1 and stored revision 2. Capture
   `redaction-recovered.png`.
4. Unmount both panels and require zero observers. Browser, context, server
   and thread close in `finally`. `proof.json` binds source hashes, traffic,
   redaction checks and cleanup; executable replay and `REPLAY.md` are retained.

Stop on exposed injected text, a changed draft, an implicit overwrite or a CSV
mismatch. This is offline transport-failure evidence, not staging or PostgreSQL
acceptance. The focused tests independently cover each error shape and numeric
boundaries. They also reproduce same-turn Read then Reload sending a stale
report request; the generation guard now rejects it before transport. Both
action orders recover through a fresh graph/report read and ignore old results.


## Blank and zero DC persistence replay

Parent X05 report persistence. The executed support fixture is
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-optional-dc-fehb_3fd/replay.py`.
Its `proof.json` binds the three product source hashes, mocked transport,
request revisions and cleanup. Use the existing React/esbuild and Python
Playwright setup. The two actual panels share one persistence client and a
synthetic file API. Saved-design reads use the one-panel 0.4 kW graph fixture.
Use the 1920 by 2200 forced-colors viewport so all controls and rows fit.

1. Read saved design in both panels. In A enter `001,0,01,1,` with DC blank.
   Hold the first Save receipt after the synthetic file commits revision 1.
   Enter explicit DC `0` before releasing the receipt. Require the newer-edit
   feedback and retained zero. Export `newer-zero.csv`. Its DC row must show
   Studio `0.4`, report `0`, delta `0.4` and FAIL. Capture
   `held-blank-newer-zero.png`.
2. Reload explicitly. Require the committed blank DC, with no report value
   or delta and UNREADABLE in `reloaded-blank.csv`. Capture
   `reloaded-blank.png`.
3. Enter zero and hold the second Save receipt after revision 2 commits.
   Clear DC before releasing it. Require newer-edit feedback and retained
   blank. Export `newer-blank.csv`, whose DC row remains UNREADABLE.
4. Reload explicitly. Require committed zero, numeric delta `0.4` and FAIL
   in `reloaded-zero.csv`. Capture `reloaded-zero.png`. Each of the four CSVs
   must equal its five normalized visible table rows, 20 row pairs total.
5. Clear DC and Save. Require expected revision 2 and committed revision 3.
   Require the complete write baseline sequence `0,1,2`.
6. Unmount both panels and require zero observers. Close context, browser,
   server and thread in `finally`. Preserve the replay, proof, synthetic file,
   CSVs and screenshots. No video is required.

Stop on a protected prompt, changed lexical value, incorrect DC verdict,
lost newer draft, CSV mismatch or wrong revision. The support fixture controls
the delayed transport receipts. Product actions use the visible controls.
This run proves offline synthetic persistence and browser rendering. Frozen
staging grading still requires its admitted window and source receipt.


## Expired Reload rejection replay

Parent X05 report persistence. The executed support fixture is
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-stale-rejection-z4ox8_u6/replay.py`.
Use its saved source hashes, synthetic project-file API and mocked saved-design
read. Two actual panels share one actual persistence client. The 1920 by 2200
forced-colors viewport shows both complete panels and their controls.

1. Read saved design in A and B. In A enter `001,0,01,1,0.4` and Save.
   Require the saved cue and synthetic file revision 1.
2. Hold A's next report GET at the mocked fetch boundary. Click Reload and
   wait for the actual 20-second timeout. Require retry feedback and an
   aborted old signal. Keep the expired promise available to reject later.
3. In B Reload revision 1, enter `003,0,01,1,0.6` and Save. Require revision 2.
   In A Reload again and require the saved-load cue and those exact values.
   Download `newer-reload.csv` through A's visible button.
4. Reject the expired predecessor using the support fixture. Require the
   accepted saved-load cue, retained `003` and `0.6`, and zero stale load-error
   messages. Download `after-expired-rejection.csv`. Both files must equal
   their five normalized visible rows and each other. The DC row shows
   Studio `0.4`, report `0.6`, delta `-0.2` and FAIL. Capture
   `newer-load-survives-rejection.png`.
5. In A enter `004,0,01,1,0.7` and Save. Require expected revision 2 and
   committed revision 3. The complete write baseline sequence is `0,1,2`.
6. Unmount both panels and require zero observers. Close context, browser,
   server and thread in `finally`. Preserve `proof.json`, the synthetic file,
   CSVs, screenshot, executable replay and this settled recipe. No video is used.

Stop on a protected prompt, stale error, changed accepted value, CSV mismatch
or wrong revision. The support fixture controls only transport timing and
synthetic material. Product actions use visible controls. This is offline
browser evidence. Frozen staging grading awaits its admitted window.


## Native enclosing-form reset replay

Parent X05 report persistence. Execute the retained support fixture at
`D:/agent-data/leaf-research/studio-xbench-2026-11/synthetic-tests/x05-native-reset-pvsfalac/replay.py`.
It bundles actual panels and a shared persistence client with synthetic file
storage and mocked saved-design reads. A native enclosing form supplies the
visible Reset enclosing form button. Use the 1920 by 2200 forced-colors view
so both complete panels and the reset button fit.

1. Read saved design in both panels. In A enter `001,0,01,1,0.4` and Save.
   Require the saved cue and file revision 1.
2. Enter `002,0,01,1,0.6` and download `before-idle-reset.csv`. Click Reset
   enclosing form. Require the same exact five input values, file revision 1
   and equal `after-idle-reset.csv`. Capture `idle-native-reset.png`.
3. Hold the next Save receipt after its synthetic file commits revision 2.
   Enter `003,0,01,1,0.7`, then click Reset enclosing form again. Require those
   newer values before and after releasing the old receipt. Require the
   newer-edits-unsaved cue and no implicit write. Download
   `held-save-reset.csv`. Its DC row must show Studio `0.4`, report `0.7`,
   delta `-0.3` and FAIL. Capture `held-save-native-reset.png`.
4. Each of the three actual CSVs must equal its five normalized DOM rows,
   15 row pairs total. Save the newer drafts explicitly. Require expected
   revision 2, committed revision 3 and the write baseline sequence `0,1,2`.
5. Unmount both panels and require zero observers. Close context, browser,
   server and thread in `finally`. Preserve the replay, proof, synthetic file,
   CSVs, screenshots and this settled recipe. No video is used.

Stop on a protected prompt, input/CSV disagreement, implicit write, lost newer
draft or incorrect CAS revision. Support setup controls the synthetic transport
receipt. Reset, Read, Save and Download use visible native controls. This run
provides offline browser evidence. Frozen staging grading awaits admission.
