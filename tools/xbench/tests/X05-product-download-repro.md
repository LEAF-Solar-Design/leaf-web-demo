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
entry = artifact / 'entry.jsx'
entry.write_text(
    'import React from ' + json.dumps((repo / 'web/node_modules/react/index.js').as_posix()) + ';\n'
    'import {createRoot} from ' + json.dumps((repo / 'web/node_modules/react-dom/client.js').as_posix()) + ';\n'
    'import Panel from ' + json.dumps(panel.as_posix()) + ';\n'
    'const binding = ' + json.dumps(binding) + ';\n'
    'const response = ' + json.dumps(response) + ';\n' + '''
let attempts = 0;
window.setSavedGraph = graph => { response.data.intake.solar_design_graph = structuredClone(graph); };
const client = {
  getSnapshot: () => ({binding, authorityKey: 'synthetic'}),
  subscribe: () => () => {},
  readContext: async () => {
    if (++attempts === 1) throw new Error('synthetic private read error');
    return response;
  },
};
createRoot(document.getElementById('root')).render(
  <Panel solarBinding={binding} authorityKey="synthetic"
    projectClient={client} isCurrent={() => true} />);
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

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    inputs = json.loads((artifact / 'meta.json').read_text(encoding='utf-8'))['inputs']
    sources = {(repo / name).resolve() for name in inputs}
    sources.add(repo / 'web/src/styles.css')
    proof = dict(synthetic=True, rows_matched=5, visible=visible, downloaded=actual,
                 malformed_graph_replays=cases,
                 narrow_zoom_replays=narrow_replays,
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
                  'malformed_graph_repairs': len(cases), 'narrow_zoom_replays': len(narrow_replays)}))
```
