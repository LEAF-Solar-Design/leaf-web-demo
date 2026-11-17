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
It also checks keyboard read/export failure announcements and retries. No auth,
video, real drawings, environment/secret reads, staging, or uploads are involved.

```python
import csv
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
    for label, value in zip(labels, ['2', '1', '2', '1', '0.8']):
        page.keyboard.press('Tab')
        field = page.get_by_role('textbox', name=label + ' engineering report/BOM', exact=True)
        expect(field).to_be_focused()
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
    with csv_path.open(newline='', encoding='utf-8') as stream:
        actual = list(csv.DictReader(stream))
    assert actual == visible, 'Actual browser download differs from the rendered comparison'
    assert actual[1] == dict(metric='optimizers', studio='', oracle='1', delta='', verdict='UNREADABLE')
    assert actual[2] == dict(metric='inverters', studio='1', oracle='2', delta='-1', verdict='FAIL')
    assert [row['verdict'] for row in actual] == ['PASS', 'UNREADABLE', 'FAIL', 'PASS', 'PASS']
    page.screenshot(path=str(artifact / 'comparison.png'), full_page=True)
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    inputs = json.loads((artifact / 'meta.json').read_text(encoding='utf-8'))['inputs']
    sources = {(repo / name).resolve() for name in inputs}
    sources.add(repo / 'web/src/styles.css')
    proof = dict(synthetic=True, rows_matched=5, visible=visible, downloaded=actual,
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
print(json.dumps({'proof': str(proof_path), 'rows_matched': 5, 'keyboard_retries_passed': 2}))
```
