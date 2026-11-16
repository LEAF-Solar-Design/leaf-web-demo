"""Supported headed X08 acquisition; native inputs never enter reports."""

from contextlib import ExitStack
import csv
import hashlib
import io
import json
from math import isfinite
from pathlib import Path
import re
import socket
import subprocess
import time
from uuid import uuid4

from benches.x08_oracle import _modules, choose_window
from xbench_core import read_health


PERMITTED_ROOT = Path('D:/agent-data/leaf-research')
MODULE_COUNT = 9463


class RuntimeSetupError(RuntimeError):
    """Only this type, never private exception text, is reportable."""


def permitted_path(value, permitted_root=PERMITTED_ROOT):
    path, root = Path(value), Path(permitted_root)
    if not path.is_absolute() or not root.is_absolute():
        raise ValueError('Absolute private path required')
    path, root = path.resolve(), root.resolve()
    if not path.is_relative_to(root):
        raise ValueError('Private path outside permitted root')
    return path


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def load_native_oracle(manifest, *, permitted_root=PERMITTED_ROOT,
                       expected_count=MODULE_COUNT):
    """Rehash the pinned native binding and recompute inventory/window from CSV.

    The signed-off receipt's assertions are consumed as assertions, not invented
    raw walk-host provenance. The binding's saved selection is never an oracle.
    """
    pin = manifest['native_oracle']
    binding_data = permitted_path(pin['binding_path'], permitted_root).read_bytes()
    if not re.fullmatch('[0-9a-f]{64}', pin['sha256']) or _digest(binding_data) != pin['sha256']:
        raise ValueError('Binding digest mismatch')
    binding = json.loads(binding_data)
    if (binding.get('source_binding_verified') is not True
            or not binding.get('source_binding_basis')
            or type(expected_count) is not int or expected_count <= 0
            or type(binding.get('module_count')) is not int
            or binding['module_count'] != expected_count):
        raise ValueError('Native source binding unavailable')
    fixtures = manifest['fixtures']
    candidates = [f for f in fixtures if f.get('sha256') == binding['fixture_sha256']]
    if len(candidates) != 1:
        raise ValueError('Ambiguous or mixed fixture')
    fixture = candidates[0]
    if type(fixture.get('modules')) is not int or fixture['modules'] != expected_count:
        raise ValueError('Fixture module count mismatch')
    fixture_path = permitted_path(fixture['path'], permitted_root)
    content = fixture_path.read_bytes()
    if (type(fixture['bytes']) is not int or type(binding['fixture_bytes']) is not int
            or not content or len(content) != fixture['bytes']
            or len(content) != binding['fixture_bytes']
            or _digest(content) != fixture['sha256']):
        raise ValueError('Fixture integrity mismatch')
    receipt = permitted_path(binding['receipt_path'], permitted_root)
    if receipt.name != 'RECEIPT.md' or not receipt.read_bytes().strip():
        raise ValueError('Native owner receipt unavailable')
    csv_data = permitted_path(receipt.parent / 'module-bbox.csv', permitted_root).read_bytes()
    script = permitted_path(receipt.parent / 'bbox_dump.lsp', permitted_root).read_bytes()
    if (_digest(csv_data) != binding['native_csv_sha256']
            or _digest(script) != binding['native_script_sha256']):
        raise ValueError('Native artifact integrity mismatch')
    reader = csv.DictReader(io.StringIO(csv_data.decode('utf-8-sig')))
    columns = reader.fieldnames
    supported = [('xmin', 'ymin', 'xmax', 'ymax'),
                 ('min_x', 'min_y', 'max_x', 'max_y'),
                 ('minx', 'miny', 'maxx', 'maxy')]
    coords = next((names for names in supported if columns and
                   {'handle', *names} <= set(columns)), None)
    if coords is None:
        raise ValueError('Unsupported native CSV schema')
    rows = _modules([dict(handle=row['handle'], bounds=[float(row[k]) for k in coords])
                     for row in reader])
    if len(rows) != expected_count or _modules(binding['modules']) != rows:
        raise ValueError('Native inventory mismatch')
    window = choose_window(rows)
    return dict(fixture_content=content, modules=rows, window=window)


class _Delegate:
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        return getattr(self._target, name)


class TrackedMouse(_Delegate):
    def __init__(self, target):
        super().__init__(target)
        self.held = set()

    def click(self, *args, **kwargs):
        button = kwargs.get('button', 'left')
        self.held.add(button)
        result = self._target.click(*args, **kwargs)
        self.held.discard(button)
        return result

    def down(self, **kwargs):
        self.held.add(kwargs.get('button', 'left'))
        return self._target.down(**kwargs)

    def up(self, **kwargs):
        result = self._target.up(**kwargs)
        self.held.discard(kwargs.get('button', 'left'))
        return result


class TrackedElement(_Delegate):
    def __init__(self, target):
        super().__init__(target)
        self.released = False

    def dispose(self):
        result = self._target.dispose()
        self.released = True
        return result


class TrackedCanvas(_Delegate):
    def __init__(self, target, page):
        super().__init__(target)
        self._page = page

    def element_handle(self, **kwargs):
        element = self._target.element_handle(**kwargs)
        if element is None:
            return None
        tracked = TrackedElement(element)
        self._page.elements.append(tracked)
        return tracked


def _unwrap(value):
    if isinstance(value, TrackedElement):
        return value._target
    if type(value) is dict:
        return {key: _unwrap(item) for key, item in value.items()}
    if type(value) is list:
        return [_unwrap(item) for item in value]
    if type(value) is tuple:
        return tuple(_unwrap(item) for item in value)
    return value


class TrackedPage(_Delegate):
    def __init__(self, target):
        super().__init__(target)
        self.mouse = TrackedMouse(target.mouse)
        self.elements = []

    def evaluate(self, expression, arg=None):
        return self._target.evaluate(expression, _unwrap(arg))


_OBSERVATION = """canvas => {
    const api = canvas.closest('.viewer-canvas')?.__leafViewerObservation;
    if (!api || typeof api.snapshot !== 'function') throw Error('Observation unavailable');
    return api.snapshot();
}"""
_PROJECT = """(canvas, point) => {
    const api = canvas.closest('.viewer-canvas')?.__leafViewerObservation;
    if (!api || typeof api.project !== 'function') throw Error('Projection unavailable');
    return api.project(point[0], point[1]);
}"""


def _optional_button(page, name):
    locator = page.get_by_role('button', name=name, exact=True)
    for index in range(locator.count()):
        button = locator.nth(index)
        if button.is_visible():
            button.click(timeout=20000)
            return True
    return False


def navigate(page, name, version_id, *, expected_drawing_key):
    page.reload(timeout=60000)
    page.wait_for_load_state('networkidle', timeout=30000)
    _optional_button(page, 'Continue workspace')
    page.get_by_text('Projects / Change project', exact=True).click(timeout=20000)
    matches = page.get_by_text(name, exact=True)
    matches.first.wait_for(state='attached', timeout=30000)
    for index in range(matches.count()):
        if matches.nth(index).is_visible():
            matches.nth(index).click(timeout=20000)
            break
    else:
        raise ValueError('Project control unavailable')
    _optional_button(page, 'Continue workspace')
    page.get_by_role('button', name='Project', exact=True).click(timeout=20000)
    page.get_by_label('Canonical drawing version').select_option(version_id, timeout=30000)
    _optional_button(page, 'Preview v1 in the drawing')
    _optional_button(page, 'Return to drawing')
    canvas = page.locator('.viewer-canvas canvas').first
    canvas.wait_for(state='visible', timeout=60000)
    page.wait_for_function("""expected => {
        const canvas = document.querySelector('.viewer-canvas canvas');
        const api = canvas?.closest('.viewer-canvas')?.__leafViewerObservation;
        const canonical = Array.from(document.querySelectorAll('select')).find(select =>
            Array.from(select.labels || []).some(label =>
                Array.from(label.childNodes).filter(node => node.nodeType === 3)
                    .map(node => node.textContent).join('').trim() ===
                    'Canonical drawing version'));
        return !!api && typeof api.snapshot === 'function' &&
            typeof api.project === 'function' &&
            api.snapshot()?.drawingKey === expected.drawingKey &&
            canonical?.value === expected.versionId &&
            canvas.getBoundingClientRect().width > 0 &&
            canvas.getBoundingClientRect().height > 0 &&
            getComputedStyle(canvas).visibility === 'visible';
    }""", arg=dict(drawingKey=expected_drawing_key, versionId=version_id), timeout=30000)
    return canvas


class LiveRuntime:
    def __init__(self, stack, page, canvas, oracle, base, *, headed, expected_drawing_key):
        self._stack, self._closed = stack, False
        self.page = TrackedPage(page)
        self.canvas = TrackedCanvas(canvas, self.page)
        self._oracle, self._base, self._headed = oracle, base, headed
        self._expected_drawing_key = expected_drawing_key
        self.expected_module_handles = [row['handle'] for row in oracle['modules']]
        self.expected_window_handles = list(oracle['window']['expected_handles'])

    def read_health(self):
        return read_health(self._base, timeout=20)

    def geometry_snapshot(self):
        snapshot = self.canvas.evaluate(_OBSERVATION)
        if not isinstance(snapshot, dict) or snapshot.get('drawingKey') != self._expected_drawing_key:
            raise ValueError('Project drawing binding unavailable (UNREADABLE)')
        return snapshot

    def _point(self, wx, wy):
        point = self.canvas.evaluate(_PROJECT, [wx, wy])
        box = self.canvas.bounding_box()
        viewport = self.page.viewport_size
        if (not isinstance(point, dict) or not isinstance(box, dict)
                or not isinstance(viewport, dict)
                or any(type(box.get(k)) not in (int, float) or not isfinite(box[k])
                       for k in ('x', 'y', 'width', 'height'))
                or box['width'] <= 0 or box['height'] <= 0
                or any(type(viewport.get(k)) not in (int, float) or not isfinite(viewport[k])
                       or viewport[k] <= 0 for k in ('width', 'height'))
                or any(type(point.get(k)) not in (int, float) or not isfinite(point[k])
                       for k in ('x', 'y'))
                or not (max(0, box['x']) < point['x'] <
                        min(viewport['width'], box['x'] + box['width']))
                or not (max(0, box['y']) < point['y'] <
                        min(viewport['height'], box['y'] + box['height']))):
            raise ValueError('Native target clipped')
        return point['x'], point['y']

    def single_action(self, index):
        bounds = self._oracle['modules'][index % len(self.expected_module_handles)]['bounds']

        def click():
            point = self._point((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
            return self.page.mouse.click(*point)
        return click

    def window_action(self, index):
        x0, y0, x1, y1 = self._oracle['window']['rect']
        def drag():
            start, end = self._point(x0, y1), self._point(x1, y0)
            if start[0] >= end[0] or start[1] >= end[1]:
                raise ValueError('Containment drag projection unavailable')
            self.page.mouse.move(*start)
            try:
                self.page.mouse.down(button='left')
                self.page.mouse.move(*end, steps=10)
            finally:
                self.page.mouse.up(button='left')
        return drag

    def ack_control(self, index):
        self.page.wait_for_function("""() => !Array.from(
            document.querySelectorAll('.strip-running .verb')).some(
            el => /queued|running/i.test(el.textContent))""", timeout=20000)
        self.page.get_by_test_id('command-bar').fill('/count-by-layer', timeout=20000)
        commands = self.page.get_by_role('listbox', name='Tool commands', exact=True)
        commands.wait_for(state='visible', timeout=20000)
        commands.get_by_role('option').filter(
            has_text=re.compile(r'(^|\s)/count-by-layer(?:\s|$)')).first.click(timeout=20000)
        control = self.page.get_by_role('button', name='Run count-by-layer', exact=True)
        control.wait_for(state='visible', timeout=20000)
        deadline = time.monotonic() + 20
        while not control.is_enabled():
            if time.monotonic() >= deadline:
                raise ValueError('Run control unavailable')
            self.page.wait_for_timeout(100)
        return control

    def frame_cleanup(self):
        absent = self.page.evaluate("""() => !window.__xbenchX08Frames &&
            !window.__xbenchX08Selection && !window.__xbenchX08Ack""")
        return dict(recorder_removed=absent is True,
                    element_disposed=all(item.released for item in self.page.elements),
                    mouse_released=not self.page.mouse.held)

    def reference(self):
        script = """$os = Get-CimInstance Win32_OperatingSystem;
        $cpu = @(Get-CimInstance Win32_Processor | ForEach-Object { $_.Name });
        $gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name });
        @{ os=$os.Caption; cpu=($cpu -join '; '); gpu=$gpu;
           free_ram_bytes=([long]$os.FreePhysicalMemory * 1024) } | ConvertTo-Json -Compress"""
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-Command', script], capture_output=True, text=True,
                                timeout=20, check=True)
        data = json.loads(result.stdout)
        data.update(hostname=socket.gethostname(),
                    chromium_version=self.page.context.browser.version,
                    headed=self._headed, viewport=self.page.viewport_size,
                    device_scale_factor=self.page.evaluate('() => window.devicePixelRatio'))
        return data

    def close(self):
        if not self._closed:
            self._closed = True
            self._stack.close()


def make_live_runtime(ctx):
    """Acquire exactly one owned session and disposable project, in that order."""
    from xbench_session import STAGING_ORIGIN, signed_in, api
    from xbench_studio import StudioClient, disposable_project

    stack = ExitStack()
    try:
        if ctx.base != STAGING_ORIGIN:
            raise ValueError('Staging required')
        permitted_path(ctx.out_dir)
        oracle = load_native_oracle(ctx.manifest)
        page, info = stack.enter_context(signed_in(
            Path(__file__).resolve().parents[3], ctx.source_sha, origin=STAGING_ORIGIN,
            headless=False, viewport=(1920, 1080)))
        if info.get('web_build') not in (None, ctx.source_sha):
            raise ValueError('Source drift')

        def transport(method, path, **kwargs):
            status, headers, body = api(page, method, path, origin=STAGING_ORIGIN, **kwargs)
            return status, (json.loads(body) if 'application/json' in
                            headers.get('content-type', '').lower() else body)

        client = StudioClient(transport)
        name = 'xbench-x08-' + uuid4().hex
        project = stack.enter_context(disposable_project(client, name))
        drawing = client.upload_drawing('x08.dwg', oracle['fixture_content'])
        client.wait_upload(drawing, timeout_s=300)
        adopted = client.adopt_drawing(project['project_id'], drawing, 'x08', version=1)
        expected_drawing_key = 'project:' + ':'.join([
            project['org_id'], project['project_id'], adopted['drawing_id'], adopted['version_id']])
        canvas = navigate(page, name, adopted['version_id'],
                          expected_drawing_key=expected_drawing_key)
        return LiveRuntime(stack, page, canvas, oracle, STAGING_ORIGIN, headed=True,
                           expected_drawing_key=expected_drawing_key)
    except BaseException as exc:
        try:
            stack.close()
        except BaseException as cleanup_exc:
            if not isinstance(cleanup_exc, Exception) and isinstance(exc, Exception):
                raise
            if isinstance(exc, Exception):
                raise RuntimeSetupError('Owned setup cleanup unavailable') from None
        if not isinstance(exc, Exception):
            raise
        raise RuntimeSetupError('Supported acquisition unavailable') from None
