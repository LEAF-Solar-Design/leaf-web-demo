"""Synthetic native artifacts and UI doubles; no live input or browser required."""

from contextlib import ExitStack, contextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest

from benches import x08_runtime as live


def digest(data):
    return hashlib.sha256(data).hexdigest()


def native(tmp_path, count=500):
    fixture = tmp_path / 'synthetic.dwg'
    fixture.write_bytes(b'synthetic only')
    csv = tmp_path / 'module-bbox.csv'
    csv.write_text('handle,xmin,ymin,xmax,ymax\n' + ''.join(
        f'h{i},{i * 2},0,{i * 2 + 1},1\n' for i in range(count)))
    script = tmp_path / 'bbox_dump.lsp'
    script.write_bytes(b'; synthetic native script')
    receipt = tmp_path / 'RECEIPT.md'
    receipt.write_text('Synthetic owner attestation for offline tests only')
    binding = dict(fixture_sha256=digest(fixture.read_bytes()),
                   fixture_bytes=fixture.stat().st_size,
                   native_csv_sha256=digest(csv.read_bytes()),
                   native_script_sha256=digest(script.read_bytes()),
                   receipt_path=str(receipt), source_binding_verified=True,
                   source_binding_basis='synthetic pre/post assertion', module_count=count,
                   modules=[dict(handle=f'h{i}', bounds=[i * 2, 0, i * 2 + 1, 1])
                            for i in range(count)],
                   window=dict(rect=[-1, -1, 0, 0], expected_handles=['FORGED']))
    path = tmp_path / 'binding.json'

    def pin():
        data = json.dumps(binding).encode()
        path.write_bytes(data)
        return dict(native_oracle=dict(binding_path=str(path), sha256=digest(data)),
                    fixtures=[dict(id='drawing', path=str(fixture),
                                   modules=count,
                                   bytes=fixture.stat().st_size,
                                   sha256=binding['fixture_sha256'])])
    return binding, pin


@pytest.mark.parametrize('count', [500, 9463])
def test_native_integrity_recomputes_window_and_production_count(tmp_path, count):
    binding, pin = native(tmp_path, count)
    manifest = pin()
    options = {} if count == 9463 else dict(expected_count=count)
    oracle = live.load_native_oracle(manifest, permitted_root=tmp_path, **options)
    assert len(oracle['modules']) == count
    assert len(oracle['window']['expected_handles']) == 500
    assert 'FORGED' not in oracle['window']['expected_handles']
    assert oracle['window']['rect'] != binding['window']['rect']
    if count == 500:
        with pytest.raises(ValueError):
            live.load_native_oracle(manifest, permitted_root=tmp_path)


@pytest.mark.parametrize('artifact', ['binding.json', 'synthetic.dwg',
                                     'module-bbox.csv', 'bbox_dump.lsp'])
def test_native_pinned_files_cannot_change(tmp_path, artifact):
    _, pin = native(tmp_path)
    manifest = pin()
    path = tmp_path / artifact
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        live.load_native_oracle(manifest, permitted_root=tmp_path, expected_count=500)


@pytest.mark.parametrize('count', [499, None, True])
def test_native_fixture_module_count_must_match(tmp_path, count):
    _, pin = native(tmp_path)
    manifest = pin()
    if count is None:
        del manifest['fixtures'][0]['modules']
    else:
        manifest['fixtures'][0]['modules'] = count
    with pytest.raises(ValueError, match='Fixture module count mismatch'):
        live.load_native_oracle(manifest, permitted_root=tmp_path, expected_count=500)


@pytest.mark.parametrize('fault', ['false-binding', 'mixed-fixture', 'duplicate', 'nan',
                                  'zero-box', 'wrong-count', 'script-digest'])
def test_native_false_mixed_or_invalid_rows_refused(tmp_path, fault):
    binding, pin = native(tmp_path)
    if fault == 'false-binding':
        binding['source_binding_verified'] = False
    elif fault == 'mixed-fixture':
        binding['fixture_sha256'] = 'a' * 64
    elif fault == 'wrong-count':
        binding['module_count'] = 499
    elif fault == 'script-digest':
        binding['native_script_sha256'] = 'a' * 64
    else:
        csv = tmp_path / 'module-bbox.csv'
        text = csv.read_text()
        replacement = {'duplicate': 'h0,2,0,3,1', 'nan': 'h1,nan,0,3,1',
                       'zero-box': 'h1,2,0,2,1'}[fault]
        csv.write_text(text.replace('h1,2,0,3,1', replacement))
        binding['native_csv_sha256'] = digest(csv.read_bytes())
    with pytest.raises(ValueError):
        live.load_native_oracle(pin(), permitted_root=tmp_path, expected_count=500)


def test_resolved_escape_and_relative_paths_refused(tmp_path):
    with pytest.raises(ValueError):
        live.permitted_path(tmp_path / '..' / 'escape', tmp_path)
    with pytest.raises(ValueError):
        live.permitted_path('relative', tmp_path)
    link = tmp_path / 'link'
    try:
        link.symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(ValueError):
        live.permitted_path(link / 'escape', tmp_path)


class Mouse:
    def __init__(self):
        self.events, self.fail = [], None

    def action(self, name, *args, **kwargs):
        self.events.append((name, args, kwargs))
        if self.fail == name:
            raise RuntimeError('synthetic failure')

    def down(self, **kwargs):
        self.action('down', **kwargs)

    def up(self, **kwargs):
        self.action('up', **kwargs)

    def move(self, *args, **kwargs):
        self.action('move', *args, **kwargs)

    def click(self, *args, **kwargs):
        self.action('click', *args, **kwargs)


class Element:
    def __init__(self):
        self.disposed, self.fail = 0, False

    def dispose(self):
        if self.fail:
            raise RuntimeError('dispose')
        self.disposed += 1


class Canvas:
    def __init__(self):
        self.elements = []

    def element_handle(self):
        element = Element()
        self.elements.append(element)
        return element

    def bounding_box(self):
        return dict(x=0, y=0, width=100, height=100)

    def evaluate(self, expression, point=None):
        if point is not None:
            return dict(x=point[0], y=100 - point[1])
        return dict(drawingKey='actual-double-snapshot')


class Page:
    def __init__(self):
        self.mouse, self.args = Mouse(), []
        self.viewport_size = dict(width=100, height=100)
        self.recorder_absent = True
        self.unrelated = Element()

    def evaluate(self, expression, arg=None):
        self.args.append(arg)
        return self.recorder_absent


def runtime():
    page, canvas, stack = Page(), Canvas(), ExitStack()
    oracle = dict(modules=[dict(handle='native-only', bounds=[10, 10, 20, 20])],
                  window=dict(rect=[10, 10, 40, 40], expected_handles=['native-only']))
    return live.LiveRuntime(stack, page, canvas, oracle, 'synthetic', headed=True), page, canvas


def test_native_projection_actual_mouse_order_and_snapshot():
    value, page, _ = runtime()
    assert value.expected_module_handles == ['native-only']
    assert value.geometry_snapshot() == dict(drawingKey='actual-double-snapshot')
    click = value.single_action(0)
    assert page.mouse.events == []
    click()
    assert page.mouse.events == [('click', (15, 85), {})]
    page.mouse.events.clear()
    value.window_action(0)()
    assert [event[0] for event in page.mouse.events] == ['move', 'down', 'move', 'up']
    assert page.mouse.events[0][1] == (10, 60)
    assert page.mouse.events[2][1] == (40, 90)
    assert value.frame_cleanup()['mouse_released'] is True


@pytest.mark.parametrize('bounds', [[-2, 10, 0, 20], [100, 10, 102, 20],
                                   [10, -2, 20, 0], [10, 100, 20, 102]])
def test_clipped_native_targets_refuse_without_mouse(bounds):
    value, page, _ = runtime()
    value._oracle['modules'][0]['bounds'] = bounds
    with pytest.raises(ValueError):
        value.single_action(0)
    assert page.mouse.events == []


def test_tracker_unwrap_dispose_and_failures_are_observed():
    value, page, canvas = runtime()
    original_mouse = page.mouse
    element = value.canvas.element_handle()
    value.page.evaluate('read', dict(canvas=element, nested=[element], ordinary={'n': 1}))
    assert page.args[-1] == dict(canvas=canvas.elements[0], nested=[canvas.elements[0]],
                                 ordinary={'n': 1})
    assert value.frame_cleanup()['element_disposed'] is False
    canvas.elements[0].fail = True
    with pytest.raises(RuntimeError):
        element.dispose()
    assert element.released is False
    canvas.elements[0].fail = False
    element.dispose()
    assert value.frame_cleanup()['element_disposed'] is True
    value.page.mouse.down(button='right')
    page.mouse.fail = 'up'
    with pytest.raises(RuntimeError):
        value.page.mouse.up(button='right')
    assert value.frame_cleanup()['mouse_released'] is False
    page.mouse.fail = None
    value.page.mouse.up(button='right')
    page.mouse.fail = 'down'
    with pytest.raises(RuntimeError):
        value.page.mouse.down()
    assert value.frame_cleanup()['mouse_released'] is True
    page.recorder_absent = False
    assert value.frame_cleanup()['recorder_removed'] is False
    assert page.mouse is original_mouse and page.unrelated.disposed == 0


@pytest.mark.parametrize('kwargs', [{}, {'button': 'right', 'delay': 17, 'click_count': 2}])
def test_successful_native_click_clears_in_flight_button_and_forwards(kwargs):
    value, page, _ = runtime()
    original = page.mouse.click
    result = object()

    def click(*args, **options):
        assert value.frame_cleanup()['mouse_released'] is False
        assert value.page.mouse.held == {kwargs.get('button', 'left')}
        original(*args, **options)
        return result

    page.mouse.click = click
    assert value.page.mouse.click(15, 85, **kwargs) is result
    assert page.mouse.events == [('click', (15, 85), kwargs)]
    assert value.frame_cleanup()['mouse_released'] is True


@pytest.mark.parametrize('kwargs', [{}, {'button': 'right', 'delay': 17, 'click_count': 2}])
def test_failed_native_click_cannot_claim_release(kwargs):
    value, page, _ = runtime()
    page.mouse.fail = 'click'
    with pytest.raises(RuntimeError, match='synthetic failure'):
        value.page.mouse.click(15, 85, **kwargs)
    assert page.mouse.events == [('click', (15, 85), kwargs)]
    assert value.page.mouse.held == {kwargs.get('button', 'left')}
    assert value.frame_cleanup()['mouse_released'] is False


def test_drag_move_failure_still_releases_owned_button():
    value, page, _ = runtime()
    action = value.window_action(0)
    original = page.mouse.move

    def fail_second(*args, **kwargs):
        if kwargs.get('steps'):
            raise RuntimeError('move')
        original(*args, **kwargs)

    page.mouse.move = fail_second
    with pytest.raises(RuntimeError):
        action()
    assert [event[0] for event in page.mouse.events] == ['move', 'down', 'up']
    assert value.frame_cleanup()['mouse_released'] is True


def test_close_idempotent_and_only_owned_stack():
    value, page, _ = runtime()
    order = []
    value._stack.callback(lambda: order.append('browser'))
    value._stack.callback(lambda: order.append('project'))
    value.close()
    value.close()
    assert order == ['project', 'browser']
    assert page.unrelated.disposed == 0


def test_ack_prepares_ui_and_returns_final_control_without_running():
    value, page, _ = runtime()
    events = []

    class Control:
        @property
        def first(self):
            return self

        def fill(self, text, **kwargs):
            events.append(('fill', text))

        def wait_for(self, **kwargs):
            events.append(('wait', kwargs['state']))

        def get_by_role(self, role, **kwargs):
            events.append(('role', role, kwargs))
            return option

        def filter(self, **kwargs):
            events.append(('filter', kwargs))
            return self

        def click(self, **kwargs):
            events.append(('pick',))

        def is_enabled(self):
            return True

    bar, commands, option, final = Control(), Control(), Control(), Control()
    page.wait_for_function = lambda expression, **kwargs: events.append(('settled', kwargs))
    page.get_by_test_id = lambda name: bar if name == 'command-bar' else None

    def role(role_name, **kwargs):
        if role_name == 'listbox':
            assert kwargs == dict(name='Tool commands', exact=True)
            return commands
        assert role_name == 'button' and kwargs == dict(name='Run count-by-layer', exact=True)
        return final

    page.get_by_role = role
    assert value.ack_control(0) is final
    assert ('fill', '/count-by-layer') in events
    assert events.count(('pick',)) == 1
    assert events[0][0] == 'settled'
    assert page.mouse.events == []


def test_navigate_exact_opener_visible_duplicate_canonical_version_and_observer():
    events = []
    project_name = 'synthetic-unique-project'

    class Control:
        def __init__(self, name, visible=True):
            self.name, self.visible = name, visible

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self

        def is_visible(self):
            return self.visible

        def click(self, **kwargs):
            assert self.visible and kwargs == dict(timeout=20000)
            events.append(('click', self.name))

        def wait_for(self, **kwargs):
            events.append(('wait', self.name, kwargs))

        def select_option(self, version, **kwargs):
            assert version == 'canonical-version' and kwargs == dict(timeout=30000)
            events.append(('version', version))

    hidden = Control('hidden-project', visible=False)
    visible = Control('visible-project')
    later = Control('later-visible-project')
    canvas = Control('canvas')

    class Matches:
        first = hidden

        def count(self):
            return 3

        def nth(self, index):
            return [hidden, visible, later][index]

    class NavigationPage:
        def reload(self, **kwargs):
            assert kwargs == dict(timeout=60000)
            events.append(('reload',))

        def wait_for_load_state(self, state, **kwargs):
            assert state == 'networkidle' and kwargs == dict(timeout=30000)
            events.append(('settled',))

        def get_by_text(self, text, *, exact):
            assert exact is True
            if text == 'Projects / Change project':
                return Control(text)
            assert text == project_name
            return Matches()

        def get_by_role(self, role, *, name, exact):
            assert role == 'button' and exact is True
            assert name in ('Continue workspace', 'Project',
                            'Preview v1 in the drawing', 'Return to drawing')
            return Control(name)

        def get_by_label(self, label):
            assert label == 'Canonical drawing version'
            return Control(label)

        def locator(self, selector):
            assert selector == '.viewer-canvas canvas'
            return canvas

        def wait_for_function(self, expression, **kwargs):
            assert kwargs == dict(timeout=30000)
            assert '__leafViewerObservation' in expression
            assert "typeof api.snapshot === 'function'" in expression
            assert "typeof api.project === 'function'" in expression
            assert 'drawingKey' in expression
            events.append(('observer',))

    assert live.navigate(NavigationPage(), project_name, 'canonical-version') is canvas
    assert events == [
        ('reload',), ('settled',), ('click', 'Continue workspace'),
        ('click', 'Projects / Change project'),
        ('wait', 'hidden-project', dict(state='attached', timeout=30000)),
        ('click', 'visible-project'), ('click', 'Continue workspace'),
        ('click', 'Project'), ('version', 'canonical-version'),
        ('click', 'Preview v1 in the drawing'), ('click', 'Return to drawing'),
        ('wait', 'canvas', dict(state='visible', timeout=60000)), ('observer',)]


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_setup_error_and_cancellation_release_project_before_browser(tmp_path, monkeypatch, failure):
    import xbench_session
    import xbench_studio

    events = []
    page = Page()

    @contextmanager
    def session(repo, source, **kwargs):
        assert kwargs['headless'] is False and kwargs['viewport'] == (1920, 1080)
        try:
            yield page, dict(web_build='a' * 40)
        finally:
            events.append('browser')

    @contextmanager
    def project(client, name):
        try:
            yield dict(project_id='owned')
        finally:
            events.append('project')

    class Client:
        def __init__(self, transport):
            pass

        def upload_drawing(self, name, content):
            assert name == 'x08.dwg' and content == b'synthetic'
            raise failure('private input must not escape')

    monkeypatch.setattr(live, 'permitted_path', lambda value: tmp_path)
    monkeypatch.setattr(live, 'load_native_oracle', lambda manifest: dict(fixture_content=b'synthetic'))
    monkeypatch.setattr(xbench_session, 'signed_in', session)
    monkeypatch.setattr(xbench_studio, 'StudioClient', Client)
    monkeypatch.setattr(xbench_studio, 'disposable_project', project)
    ctx = SimpleNamespace(base=xbench_session.STAGING_ORIGIN, source_sha='a' * 40,
                          out_dir=tmp_path, manifest={})
    expected = live.RuntimeSetupError if failure is RuntimeError else KeyboardInterrupt
    with pytest.raises(expected) as caught:
        live.make_live_runtime(ctx)
    assert events == ['project', 'browser']
    if failure is RuntimeError:
        assert 'private input' not in str(caught.value)
    assert page.unrelated.disposed == 0


def test_reference_uses_bounded_actual_cim_browser_and_viewport(monkeypatch):
    value, page, _ = runtime()
    page.context = SimpleNamespace(browser=SimpleNamespace(version='observed-browser'))
    page.evaluate = lambda expression, arg=None: 1.25
    monkeypatch.setattr(live.socket, 'gethostname', lambda: 'other-host')

    def cim(command, **kwargs):
        assert command[:3] == ['powershell.exe', '-NoProfile', '-NonInteractive']
        assert 'Win32_OperatingSystem' in command[-1]
        assert 'Win32_Processor' in command[-1] and 'Win32_VideoController' in command[-1]
        assert '$env:' not in command[-1]
        assert kwargs == dict(capture_output=True, text=True, timeout=20, check=True)
        return SimpleNamespace(stdout=json.dumps(dict(
            os='Windows 11', cpu='observed CPU', gpu=['observed GPU'], free_ram_bytes=4096)))

    monkeypatch.setattr(live.subprocess, 'run', cim)
    reference = value.reference()
    assert reference['hostname'] == 'other-host'
    assert reference['chromium_version'] == 'observed-browser'
    assert reference['device_scale_factor'] == 1.25
    assert reference['viewport'] == page.viewport_size
    assert reference['headed'] is True
    from benches.x08_metrics import score
    assert score(dict(reference=reference), 1)[5].verdict == 'FAIL'


def test_successful_factory_uses_supported_transport_and_owns_until_close(tmp_path, monkeypatch):
    import xbench_session
    import xbench_studio

    events = []
    page, canvas = Page(), Canvas()
    oracle = dict(fixture_content=b'synthetic',
                  modules=[dict(handle='native', bounds=[1, 1, 2, 2])],
                  window=dict(rect=[1, 1, 2, 2], expected_handles=['native']))

    @contextmanager
    def session(*args, **kwargs):
        events.append('session')
        try:
            yield page, dict(web_build='a' * 40)
        finally:
            events.append('browser-close')

    @contextmanager
    def project(client, name):
        assert name.startswith('xbench-x08-')
        events.append('project')
        try:
            yield dict(project_id='owned-project')
        finally:
            events.append('project-delete')

    class Client:
        def __init__(self, transport):
            assert transport('GET', '/api/example', timeout_s=20) == (200, {'ok': True})

        def upload_drawing(self, name, content):
            assert (name, content) == ('x08.dwg', b'synthetic')
            events.append('upload')
            return 'uploaded'

        def wait_upload(self, drawing, **kwargs):
            assert drawing == 'uploaded' and kwargs == dict(timeout_s=300)
            events.append('ready')

        def adopt_drawing(self, project_id, drawing, name, **kwargs):
            assert (project_id, drawing, name) == ('owned-project', 'uploaded', 'x08')
            assert kwargs == dict(version=1)
            events.append('adopt')
            return dict(version_id='canonical-version')

    def api(actual_page, method, path, **kwargs):
        assert actual_page is page and method == 'GET' and path == '/api/example'
        assert kwargs == dict(origin=xbench_session.STAGING_ORIGIN, timeout_s=20)
        return 200, {'content-type': 'application/json'}, b'{"ok":true}'

    def navigate(actual_page, name, version):
        assert actual_page is page and version == 'canonical-version'
        events.append('navigate')
        return canvas

    monkeypatch.setattr(live, 'permitted_path', lambda value: tmp_path)
    monkeypatch.setattr(live, 'load_native_oracle', lambda manifest: oracle)
    monkeypatch.setattr(live, 'navigate', navigate)
    monkeypatch.setattr(xbench_session, 'signed_in', session)
    monkeypatch.setattr(xbench_session, 'api', api)
    monkeypatch.setattr(xbench_studio, 'StudioClient', Client)
    monkeypatch.setattr(xbench_studio, 'disposable_project', project)
    value = live.make_live_runtime(SimpleNamespace(base=xbench_session.STAGING_ORIGIN,
        source_sha='a' * 40, manifest={}, out_dir=tmp_path))
    assert events == ['session', 'project', 'upload', 'ready', 'adopt', 'navigate']
    assert value.expected_module_handles == ['native']
    value.close()
    value.close()
    assert events[-2:] == ['project-delete', 'browser-close']
    assert page.unrelated.disposed == 0
