"""Synthetic native artifacts and UI doubles; no live input or browser required."""

from contextlib import ExitStack, contextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest

from benches import x08_runtime as live


ORG_ID = '11111111-1111-4111-8111-111111111111'
PROJECT_ID = '22222222-2222-4222-8222-222222222222'
DRAWING_ID = '33333333-3333-4333-8333-333333333333'
VERSION_ID = '44444444-4444-4444-8444-444444444444'
PROJECT_KEY = f'project:{ORG_ID}:{PROJECT_ID}:{DRAWING_ID}:{VERSION_ID}'


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
    return live.LiveRuntime(stack, page, canvas, oracle, 'synthetic', headed=True,
                            expected_drawing_key='actual-double-snapshot'), page, canvas


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
        value.single_action(0)()
    assert page.mouse.events == []


@pytest.mark.parametrize('kind', ['single', 'window'])
@pytest.mark.parametrize('clipped', [False, True])
def test_actions_project_latest_layout_at_invocation(kind, clipped):
    value, page, canvas = runtime()
    original = canvas.evaluate
    projections = []
    offset = 0

    def project(expression, point=None):
        projections.append(point)
        result = original(expression, point)
        result['x'] += offset
        return result

    canvas.evaluate = project
    action = value.single_action(0) if kind == 'single' else value.window_action(0)
    assert projections == [] and page.mouse.events == []
    offset = (90 if kind == 'single' else 70) if clipped else 5
    canvas.bounding_box = lambda: dict(x=offset, y=0, width=100, height=100)
    if clipped:
        # The window's first endpoint remains visible; the second is clipped.
        with pytest.raises(ValueError, match='Native target clipped'):
            action()
        assert page.mouse.events == []
    else:
        action()
        if kind == 'single':
            assert projections == [[15, 15]]
            assert page.mouse.events == [('click', (20, 85), {})]
        else:
            assert projections == [[10, 40], [40, 10]]
            assert page.mouse.events == [('move', (15, 60), {}), ('down', (), {'button': 'left'}),
                                         ('move', (45, 90), {'steps': 10}),
                                         ('up', (), {'button': 'left'})]


@pytest.mark.parametrize('key', [None, 'old-scene', 'engine:opaque',
                               PROJECT_KEY.replace(ORG_ID, PROJECT_ID),
                               PROJECT_KEY.replace(PROJECT_ID, DRAWING_ID),
                               PROJECT_KEY.replace(DRAWING_ID, VERSION_ID),
                               PROJECT_KEY.replace(VERSION_ID, ORG_ID)])
def test_geometry_refuses_late_drawing_transition(key):
    value, _, canvas = runtime()
    value._expected_drawing_key = PROJECT_KEY
    canvas.evaluate = lambda expression: dict(drawingKey=PROJECT_KEY)
    assert value.geometry_snapshot() == dict(drawingKey=PROJECT_KEY)
    canvas.evaluate = lambda expression: dict(drawingKey=key)
    with pytest.raises(ValueError, match='UNREADABLE'):
        value.geometry_snapshot()


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
    assert value.frame_cleanup()['mouse_released'] is False
    page.recorder_absent = False
    assert value.frame_cleanup()['recorder_removed'] is False
    assert page.mouse is original_mouse and page.unrelated.disposed == 0


def test_successful_native_down_forwards_and_retains_button():
    value, page, _ = runtime()
    result = object()
    original = page.mouse.down

    def down(**kwargs):
        assert value.page.mouse.held == {'right'}
        original(**kwargs)
        return result

    page.mouse.down = down
    assert value.page.mouse.down(button='right', click_count=2) is result
    assert page.mouse.events == [('down', (), {'button': 'right', 'click_count': 2})]
    assert value.frame_cleanup()['mouse_released'] is False


@pytest.mark.parametrize('kwargs', [{}, {'button': 'right'}])
def test_post_dispatch_down_failure_retains_witness_until_successful_up(kwargs):
    value, page, _ = runtime()
    button = kwargs.get('button', 'left')
    original = page.mouse.down
    dispatched = set()

    def down(**options):
        assert value.page.mouse.held == {button}
        assert value.frame_cleanup()['mouse_released'] is False
        original(**options)
        dispatched.add(button)
        raise RuntimeError('after dispatch')

    page.mouse.down = down
    with pytest.raises(RuntimeError, match='after dispatch'):
        value.page.mouse.down(**kwargs)
    assert dispatched == {button}
    assert page.mouse.events == [('down', (), kwargs)]
    assert value.page.mouse.held == {button}
    assert value.frame_cleanup()['mouse_released'] is False
    page.mouse.fail = 'up'
    with pytest.raises(RuntimeError):
        value.page.mouse.up(**kwargs)
    assert value.page.mouse.held == {button}
    assert value.frame_cleanup()['mouse_released'] is False
    page.mouse.fail = None
    result = object()
    original_up = page.mouse.up

    def up(**options):
        original_up(**options)
        dispatched.discard(button)
        return result

    page.mouse.up = up
    assert value.page.mouse.up(**kwargs) is result
    assert not dispatched
    assert value.frame_cleanup()['mouse_released'] is True


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
            assert role == 'option'
            matcher = kwargs['name']
            # PromptBox's aria-hidden fallback glyph prefixes textContent.
            # Its accessible option name still starts with the route label.
            assert matcher.search('/count-by-layer · Count entities by layer')
            assert not matcher.search('/count-by-layer-extra · Other command')
            assert not matcher.search('/other-count-by-layer · Other command')
            return option

        def filter(self, **kwargs):
            events.append(('filter', kwargs))
            return self

        def click(self, **kwargs):
            events.append(('pick',))

        def is_enabled(self, **kwargs):
            return True

        def is_visible(self, **kwargs):
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


@pytest.mark.parametrize('observations, ready', [
    ([(PROJECT_KEY, VERSION_ID)], True),
    ([('old-scene', VERSION_ID), (PROJECT_KEY, VERSION_ID)], True),
    ([(PROJECT_KEY, 'old-version'), (PROJECT_KEY, VERSION_ID)], True),
    ([('old-scene', VERSION_ID)], False),
    ([('engine:opaque', VERSION_ID)], False),
    ([(PROJECT_KEY.replace(ORG_ID, PROJECT_ID), VERSION_ID)], False),
    ([(PROJECT_KEY.replace(PROJECT_ID, DRAWING_ID), VERSION_ID)], False),
    ([(PROJECT_KEY.replace(DRAWING_ID, VERSION_ID), VERSION_ID)], False),
    ([(PROJECT_KEY.replace(VERSION_ID, ORG_ID), VERSION_ID)], False),
    ([(PROJECT_KEY, 'old-version')], False),
])
def test_navigate_exact_opener_visible_duplicate_canonical_version_and_observer(observations, ready):
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
            assert version == VERSION_ID and kwargs == dict(timeout=30000)
            events.append(('version', version))

        def get_by_role(self, role, *, name):
            assert self.name == 'version-row' and role == 'button'
            assert name.fullmatch('Preview v7 in the drawing')
            return Control('Preview v7 in the drawing')

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
            if selector == f'li[data-element-id="version:{VERSION_ID}"]':
                return Control('version-row')
            assert selector == '.viewer-canvas canvas'
            return canvas

        def wait_for_function(self, expression, **kwargs):
            assert kwargs == dict(timeout=30000,
                                  arg=dict(drawingKey=PROJECT_KEY, versionId=VERSION_ID))
            assert '__leafViewerObservation' in expression
            assert "typeof api.snapshot === 'function'" in expression
            assert "typeof api.project === 'function'" in expression
            assert 'api.snapshot()?.drawingKey === expected.drawingKey' in expression
            assert 'canonical?.value === expected.versionId' in expression
            assert "'Canonical drawing version'" in expression
            assert "document.querySelectorAll('select')" in expression
            assert 'select.labels' in expression and 'label.childNodes' in expression
            assert 'getBoundingClientRect()' in expression
            # Model polling the two real values; earlier nonempty keys do not settle.
            for key, version in observations:
                if key == kwargs['arg']['drawingKey'] and version == kwargs['arg']['versionId']:
                    break
            else:
                raise TimeoutError('adopted drawing never became ready')
            events.append(('observer',))

    if not ready:
        with pytest.raises(TimeoutError):
            live.navigate(NavigationPage(), project_name, VERSION_ID,
                          expected_drawing_key=PROJECT_KEY)
        assert ('observer',) not in events
        return
    assert live.navigate(NavigationPage(), project_name, VERSION_ID,
                         expected_drawing_key=PROJECT_KEY) is canvas
    assert events == [
        ('reload',), ('settled',), ('click', 'Continue workspace'),
        ('click', 'Projects / Change project'),
        ('wait', 'hidden-project', dict(state='attached', timeout=30000)),
        ('click', 'visible-project'), ('click', 'Continue workspace'),
        ('click', 'Project'), ('version', VERSION_ID),
        ('click', 'Preview v7 in the drawing'), ('click', 'Return to drawing'),
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
            yield dict(org_id=ORG_ID, project_id=PROJECT_ID)
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
            yield dict(org_id=ORG_ID, project_id=PROJECT_ID)
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
            assert (project_id, drawing, name) == (PROJECT_ID, 'uploaded', 'x08')
            assert kwargs == dict(version=1)
            events.append('adopt')
            return dict(drawing_id=DRAWING_ID, version_id=VERSION_ID)

    def api(actual_page, method, path, **kwargs):
        assert actual_page is page and method == 'GET' and path == '/api/example'
        assert kwargs == dict(origin=xbench_session.STAGING_ORIGIN, timeout_s=20)
        return 200, {'content-type': 'application/json'}, b'{"ok":true}'

    def navigate(actual_page, name, version, *, expected_drawing_key):
        assert actual_page is page and version == VERSION_ID
        assert expected_drawing_key == PROJECT_KEY
        events.append('navigate')
        return canvas

    monkeypatch.setattr(live, 'permitted_path', lambda value: tmp_path)
    monkeypatch.setattr(live, 'load_native_oracle', lambda manifest: oracle)
    def frame(runtime):
        assert runtime._oracle is oracle and runtime._expected_drawing_key == PROJECT_KEY
        events.append('frame')
        return {'before_cold_samples': True}

    monkeypatch.setattr(live, 'frame_native_modules', frame)
    monkeypatch.setattr(live, 'navigate', navigate)
    monkeypatch.setattr(xbench_session, 'signed_in', session)
    monkeypatch.setattr(xbench_session, 'api', api)
    monkeypatch.setattr(xbench_studio, 'StudioClient', Client)
    monkeypatch.setattr(xbench_studio, 'disposable_project', project)
    value = live.make_live_runtime(SimpleNamespace(base=xbench_session.STAGING_ORIGIN,
        source_sha='a' * 40, manifest={}, out_dir=tmp_path))
    assert events == ['session', 'project', 'upload', 'ready', 'adopt', 'navigate', 'frame']
    assert value.expected_module_handles == ['native']
    assert value._expected_drawing_key == PROJECT_KEY
    assert dict(value.expected_run_binding) == dict(org_id=ORG_ID, project_id=PROJECT_ID,
        drawing_id=DRAWING_ID, version_id=VERSION_ID, tool='count-by-layer')
    with pytest.raises(TypeError):
        value.expected_run_binding['project_id'] = 'foreign'
    canvas.evaluate = lambda expression: dict(drawingKey=PROJECT_KEY)
    assert value.geometry_snapshot() == dict(drawingKey=PROJECT_KEY)
    value.close()
    value.close()
    assert events[-2:] == ['project-delete', 'browser-close']
    assert page.unrelated.disposed == 0


RETAINED = dict(retained_org_id=ORG_ID, retained_project_id=PROJECT_ID,
                retained_drawing_id=DRAWING_ID, retained_version_id=VERSION_ID,
                retained_intake_sha256='b' * 64)


@pytest.mark.parametrize('field', RETAINED)
@pytest.mark.parametrize('fault', ['missing', 'malformed'])
def test_invalid_retained_options_stop_before_setup(monkeypatch, field, fault):
    import xbench_session

    options = RETAINED.copy()
    if fault == 'missing':
        del options[field]
    else:
        options[field] = 'invalid'
    monkeypatch.setattr(live, 'permitted_path', lambda *_: pytest.fail('setup started'))
    ctx = SimpleNamespace(base=xbench_session.STAGING_ORIGIN, out_dir='unused', options=options)
    with pytest.raises(live.RuntimeSetupError):
        live.make_live_runtime(ctx)


@pytest.mark.parametrize('fault', [None, 'project.org_id', 'project.project_id',
                                  'organization_id', 'project_id', 'drawing_id',
                                  'input_version_id', 'intake_sha256'])
def test_retained_factory_checks_server_binding_before_input(monkeypatch, tmp_path, fault):
    import xbench_session
    import xbench_studio

    events = []
    page, canvas = Page(), Canvas()

    @contextmanager
    def session(*args, **kwargs):
        try:
            yield page, dict(web_build='a' * 40)
        finally:
            events.append('close')

    class Client:
        def __init__(self, transport):
            pass

        def project(self, project_id):
            assert project_id == PROJECT_ID
            events.append('project')
            row = dict(name='Kept project', org_id=ORG_ID, project_id=PROJECT_ID)
            if fault and fault.startswith('project.'):
                row[fault.split('.')[1]] = VERSION_ID
            return row

        def drawing_context(self, project_id, version_id, drawing_id):
            assert (project_id, version_id, drawing_id) == (PROJECT_ID, VERSION_ID, DRAWING_ID)
            events.append('context')
            row = dict(organization_id=ORG_ID, project_id=PROJECT_ID, drawing_id=DRAWING_ID,
                       input_version_id=VERSION_ID, intake_sha256='b' * 64, is_head=False,
                       head_version_id='55555555-5555-4555-8555-555555555555')
            if fault:
                row[fault] = 'c' * 64 if fault == 'intake_sha256' else row['head_version_id']
            return dict(context=row)

    def navigate(actual_page, name, version, *, expected_drawing_key):
        assert (actual_page, name, version, expected_drawing_key) == (
            page, 'Kept project', VERSION_ID, PROJECT_KEY)
        events.append('navigate')
        return canvas

    monkeypatch.setattr(xbench_session, 'signed_in', session)
    monkeypatch.setattr(xbench_studio, 'StudioClient', Client)
    monkeypatch.setattr(xbench_studio, 'disposable_project', lambda *_: pytest.fail('mutation'))
    monkeypatch.setattr(live, 'permitted_path', lambda value: tmp_path)
    monkeypatch.setattr(live, 'load_native_oracle', lambda _: dict(modules=[],
                        window=dict(expected_handles=[])))
    monkeypatch.setattr(live, 'navigate', navigate)
    monkeypatch.setattr(live, 'frame_native_modules', lambda _: events.append('frame'))
    ctx = SimpleNamespace(base=xbench_session.STAGING_ORIGIN, out_dir=tmp_path, manifest={},
                          source_sha='a' * 40, options=RETAINED)
    if fault:
        with pytest.raises(live.RuntimeSetupError):
            live.make_live_runtime(ctx)
        reads = ['project'] if fault.startswith('project.') else ['project', 'context']
        assert events == reads + ['close']
    else:
        value = live.make_live_runtime(ctx)
        assert dict(value.expected_run_binding) == dict(org_id=ORG_ID, project_id=PROJECT_ID,
            drawing_id=DRAWING_ID, version_id=VERSION_ID, tool='count-by-layer')
        value.close()
        assert events == ['project', 'context', 'navigate', 'frame', 'close']


@pytest.mark.parametrize('mode', ['cumulative', 'ready', 'disabled', 'hidden', 'late', 'stalled'])
def test_ack_preparation_uses_one_budget_and_returns_only_ready_control(mode):
    value, page, _ = runtime()
    native = [0.0]
    value._clock = lambda: native[0]
    waits, clicks = [], []
    cost = 8 if mode == 'cumulative' else 2

    def consume(timeout):
        assert 0 < timeout <= 20000
        waits.append(timeout)
        native[0] += min(cost, timeout / 1000)
        if timeout / 1000 <= cost:
            raise TimeoutError('synthetic preparation timeout')

    class Control:
        @property
        def first(self):
            return self

        def fill(self, text, *, timeout):
            assert text == '/count-by-layer'
            consume(timeout)

        def wait_for(self, *, state, timeout):
            assert state == 'visible'
            consume(timeout)

        def get_by_role(self, role, **kwargs):
            assert role == 'option'
            assert kwargs['name'].search('/count-by-layer · Count entities')
            return self

        def filter(self, **kwargs):
            return self

        def click(self, *, timeout):
            clicks.append('pick-command')
            consume(timeout)

        def is_visible(self, *, timeout):
            assert timeout <= (20 - native[0]) * 1000
            return mode != 'hidden'

        def is_enabled(self, *, timeout):
            assert timeout <= (20 - native[0]) * 1000
            if mode == 'late':
                native[0] = 20
                return True
            return mode not in ('disabled', 'stalled')

    control = Control()
    page.wait_for_function = lambda expression, *, timeout: consume(timeout)
    page.get_by_test_id = lambda name: control
    page.get_by_role = lambda *args, **kwargs: control
    page.wait_for_timeout = lambda delay: native.__setitem__(
        0, native[0] + (0 if mode == 'stalled' else delay / 1000))
    if mode == 'ready':
        assert value.ack_control(0) is control
        assert native[0] == 10
    else:
        with pytest.raises(TimeoutError):
            value.ack_control(0)
        assert native[0] <= 20
    if mode == 'cumulative':
        assert waits == [20000, 12000, 4000]
        assert clicks == []
    else:
        assert clicks == ['pick-command']
    assert page.mouse.events == []  # No Run click, hence no submitted jobs.


@pytest.mark.parametrize('bad', [-1, True, float('nan'), float('inf'), 20])
def test_ack_preparation_clock_failure_refuses_before_next_ui_callback(bad):
    value, page, _ = runtime()
    native = [0]
    value._clock = lambda: native[0]
    calls = []

    def settled(expression, *, timeout):
        calls.append(timeout)
        native[0] = bad

    page.wait_for_function = settled
    page.get_by_test_id = lambda name: pytest.fail('expired preparation continued')
    with pytest.raises(TimeoutError if bad == 20 else ValueError):
        value.ack_control(0)
    assert calls == [20000]
    assert page.mouse.events == []
