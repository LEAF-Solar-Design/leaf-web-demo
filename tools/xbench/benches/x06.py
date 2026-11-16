"""X06 independent steel takeoff comparisons and first-failure UI grader.

This bounded collector can report a proven authoring failure. It cannot PASS
until publication, spreadsheet and second-session evidence are collected.
"""
import base64
import binascii
import csv
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from xbench_core import Step, aggregate, write_json_atomic

REQUEST = ('Make me a steel takeoff: for every layer whose name mentions steel, '
           'beam or joist, how many line members are on it and their total '
           'length. Give me a table and a spreadsheet.')
ORACLE_SHA256 = 'b8c569661a6b0a7b4022bdf7ab3d5a924995359d09e773c6d508ca2f69c8b2e0'
MAX_EXACT_COUNT = (1 << 53) - 1  # Browser table integers must remain exact.
MAX_CSV_BYTES = 8 * 1024 * 1024
LIMITS = ('First-failure collector and independent comparison primitives. '
          'Resumes the lane-owned fresh project after normal fixture intake. '
          'Downstream collection remains unbuilt until authoring succeeds. '
          'No missing or unobserved acceptance step passes.')


def load_oracle(path, fixtures):
    if not isinstance(fixtures, (list, tuple)):
        raise ValueError('Invalid fixture drawing set')
    seen = set()
    for fixture in fixtures:
        drawing_id = fixture.get('id') if isinstance(fixture, dict) else None
        if not isinstance(drawing_id, str) or drawing_id not in {'first', 'second'}:
            raise ValueError('Unknown fixture drawing ID')
        if drawing_id in seen:
            raise ValueError('Duplicate fixture drawing ID: ' + drawing_id)
        seen.add(drawing_id)
    if seen != {'first', 'second'}:
        raise ValueError('Missing fixture drawing ID')
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ORACLE_SHA256:
        raise ValueError('Frozen X06 oracle hash differs')
    oracle = json.loads(raw)
    if oracle.get('schema') != 'leaf.xbench.x06-oracle.v1':
        raise ValueError('Invalid X06 oracle schema')
    drawings = oracle['drawings']
    if set(drawings) != {'first', 'second'}:
        raise ValueError('Both frozen drawings are required')
    for fixture in fixtures:
        expected = drawings[fixture['id']]
        if expected['sha256'] != fixture['sha256']:
            raise ValueError('Oracle drawing differs from manifest')
        normalize_rows(expected['rows'])
    return drawings


def _nonnegative_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Number must be finite and nonnegative')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError('Number must be finite and nonnegative') from None
    if not math.isfinite(number) or number < 0:
        raise ValueError('Number must be finite and nonnegative')
    return number


def normalize_rows(rows):
    """Reject duplicates, nonfinite lengths, fractional counts and empty tables."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError('Schedule rows must be a list or tuple')
    normalized = {}
    for row in rows:
        if not isinstance(row, dict) or not {'layer', 'count', 'total_length'} <= row.keys():
            raise ValueError('Schedule row must contain layer, count and total_length')
        layer = row['layer']
        count = row['count']
        length = row['total_length']
        if not isinstance(layer, str) or not layer or layer in normalized:
            raise ValueError('Invalid or duplicate layer')
        if type(count) is not int or not 0 <= count <= MAX_EXACT_COUNT:
            raise ValueError('Count must be an exact nonnegative browser integer')
        normalized[layer] = (count, _nonnegative_number(length))
    if not normalized:
        raise ValueError('Schedule is empty')
    return normalized


def compare_rows(observed, expected):
    actual, oracle = normalize_rows(observed), normalize_rows(expected)
    problems = []
    if set(actual) != set(oracle):
        problems.append('Layer set differs')
    for layer in sorted(set(actual) & set(oracle)):
        count, length = actual[layer]
        wanted_count, wanted_length = oracle[layer]
        if count != wanted_count:
            problems.append(layer + ': member count differs')
        difference = abs(length - wanted_length)
        if not math.isfinite(difference) or difference > wanted_length * 0.005:
            problems.append(layer + ': total length differs by more than 0.5 percent')
    return problems


def _sha256(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def intake_verdict(evidence, first_fixture):
    """Validate normalized creation, normal-control intake and request receipts."""
    if (not isinstance(evidence, dict) or not isinstance(first_fixture, dict)
            or first_fixture.get('id') != 'first' or not _sha256(first_fixture.get('sha256'))):
        return 'UNREADABLE'
    text_keys = ('project_id', 'created_project_id', 'import_project_id', 'project_name', 'request')
    bool_keys = ('fresh_project', 'normal_import', 'normal_author_ui', 'user_code_typed')
    turns = evidence.get('clarification_turns')
    if (not all(_text(evidence.get(key)) for key in text_keys)
            or not all(type(evidence.get(key)) is bool for key in bool_keys)
            or not _sha256(evidence.get('drawing_sha256'))
            or type(turns) is not int or turns < 0):
        return 'UNREADABLE'
    if (evidence['project_id'] != evidence['created_project_id']
            or evidence['project_id'] != evidence['import_project_id']
            or evidence['project_name'] != 'XBench X06'
            or evidence['drawing_sha256'] != first_fixture['sha256']
            or evidence['request'] != REQUEST or turns > 2
            or not all(evidence[key] for key in bool_keys[:-1]) or evidence['user_code_typed']):
        return 'FAIL'
    return 'PASS'


def publication_verdict(chain):
    """Bind staged source, review, publication and the first completed run."""
    if not isinstance(chain, dict) or not isinstance(chain.get('staged'), dict):
        return 'UNREADABLE'
    staged = chain['staged']
    if (not _text(staged.get('change_set_id')) or not _text(staged.get('source'))
            or not _sha256(staged.get('source_sha256'))):
        return 'UNREADABLE'
    try:
        source_digest = hashlib.sha256(staged['source'].encode('utf-8')).hexdigest()
    except UnicodeError:
        return 'UNREADABLE'
    if source_digest != staged['source_sha256']:
        return 'FAIL'
    tool_id = None
    for name, key, success in [('reviewed', 'decision', 'approved'),
                               ('published', 'status', 'published'), ('run', 'status', 'complete')]:
        record = chain.get(name)
        if (not isinstance(record, dict) or not _text(record.get('change_set_id'))
                or not _sha256(record.get('source_sha256'))):
            return 'UNREADABLE'
        if (record['change_set_id'] != staged['change_set_id']
                or record['source_sha256'] != staged['source_sha256']):
            return 'FAIL'
        if record.get(key) in ('denied', 'failed'):
            return 'FAIL'
        if record.get(key) != success:
            return 'UNREADABLE'
        if name == 'published':
            tool_id = record.get('tool_id')
            if not _text(tool_id):
                return 'UNREADABLE'
        if name == 'run':
            if not _text(record.get('tool_id')) or not _text(record.get('run_id')):
                return 'UNREADABLE'
            if record['tool_id'] != tool_id:
                return 'FAIL'
    return 'PASS'


def schedule_verdict(evidence, drawing, *, expected_units='inches'):
    """Validate collector-normalized table identity and units before its rows."""
    if (not isinstance(evidence, dict) or not isinstance(drawing, dict)
            or not _sha256(drawing.get('sha256'))
            or not isinstance(expected_units, str) or not expected_units.strip()):
        return 'UNREADABLE'
    digest, units = evidence.get('drawing_sha256'), evidence.get('length_units')
    if not _sha256(digest) or not isinstance(units, str) or not units.strip():
        return 'UNREADABLE'
    if digest != drawing['sha256'] or units != expected_units:
        return 'FAIL'
    try:
        problems = compare_rows(evidence.get('rows'), drawing.get('rows'))
    except ValueError:
        return 'UNREADABLE'
    return 'FAIL' if problems else 'PASS'


def csv_equals_table(content, table_cells):
    """Exact cells; None denotes an unreadable or oversized CSV export."""
    if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_CSV_BYTES:
        return None
    if not table_cells:
        return False
    try:
        reader = csv.reader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
        end = object()
        for expected_row in table_cells:
            if next(reader, end) != expected_row:
                return False
        # Read one extra row to reject trailing data without materializing an
        # arbitrarily long list of rows from an invalid export.
        return next(reader, end) is end
    except (UnicodeDecodeError, csv.Error):
        return None


def spreadsheet_verdict(artifact, table_cells):
    """Check a normalized CSV download envelope, then its exact display cells."""
    if (not isinstance(artifact, dict) or not isinstance(table_cells, list)
            or not table_cells or not all(isinstance(row, list) and row
                and all(isinstance(cell, str) for cell in row) for row in table_cells)):
        return 'UNREADABLE'
    size, mime, encoded = artifact.get('bytes'), artifact.get('mime'), artifact.get('base64')
    if (type(size) is not int or not 0 <= size <= MAX_CSV_BYTES
            or not isinstance(mime, str) or mime.split(';', 1)[0].strip().lower() != 'text/csv'
            or not isinstance(encoded, str) or len(encoded) > 4 * ((size + 2) // 3)):
        return 'UNREADABLE'
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return 'UNREADABLE'
    if len(content) != size:
        return 'UNREADABLE'
    equal = csv_equals_table(content, table_cells)
    if equal is None:
        return 'UNREADABLE'
    return 'PASS' if equal else 'FAIL'


def reuse_verdict(first_run, second_run, second_fixture):
    """Check saved-tool/session binding; second-drawing rows need a separate check."""
    if (not isinstance(first_run, dict) or not isinstance(second_run, dict)
            or not isinstance(second_fixture, dict) or second_fixture.get('id') != 'second'
            or not _sha256(second_fixture.get('sha256'))):
        return 'UNREADABLE'
    values = [run.get(key) for run in (first_run, second_run)
              for key in ('published_tool_id', 'session_id')]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return 'UNREADABLE'
    if not _sha256(second_run.get('drawing_sha256')):
        return 'UNREADABLE'
    if (first_run['published_tool_id'] != second_run['published_tool_id']
            or first_run['session_id'] == second_run['session_id']
            or second_run['drawing_sha256'] != second_fixture['sha256']):
        return 'FAIL'
    return 'PASS'


def second_run_verdict(first_run, second_run, second_fixture, second_oracle):
    binding = reuse_verdict(first_run, second_run, second_fixture)
    if binding != 'PASS':
        return binding
    if (not isinstance(second_oracle, dict)
            or second_oracle.get('sha256') != second_fixture.get('sha256')):
        return 'UNREADABLE'
    return schedule_verdict(second_run, second_oracle)


def provenance_verdict(evidence, published_tool, run):
    """Bind authored bytes and engine provenance to one published tool/run."""
    if not all(isinstance(item, dict) for item in (evidence, published_tool, run)):
        return 'UNREADABLE'
    if (not all(_text(item.get('tool_id')) for item in (evidence, published_tool, run))
            or not all(_sha256(item.get('source_sha256')) for item in (evidence, published_tool, run))
            or not all(_text(item.get('run_id')) for item in (evidence, run))
            or not _text(evidence.get('engine_run_id')) or not _text(evidence.get('source'))):
        return 'UNREADABLE'
    try:
        digest = hashlib.sha256(evidence['source'].encode('utf-8')).hexdigest()
    except UnicodeError:
        return 'UNREADABLE'
    if (any(item['tool_id'] != published_tool['tool_id'] for item in (evidence, run))
            or any(item['source_sha256'] != digest for item in (evidence, published_tool, run))
            or evidence['run_id'] != run['run_id'] or evidence['engine_run_id'] != run['run_id']):
        return 'FAIL'
    clean = no_solar_provenance(evidence['source'], evidence.get('engines'))
    return 'UNREADABLE' if clean is None else ('PASS' if clean else 'FAIL')


def no_solar_provenance(source, engines):
    """Missing provenance cannot prove absence of a solar execution path."""
    if not isinstance(source, str) or not source.strip():
        return None
    if not isinstance(engines, list) or not engines or not all(
            isinstance(item, str) and item.strip() for item in engines):
        return None
    return re.search(r'solar', source + '\n' + '\n'.join(engines), re.I) is None


def budget_ok(seconds, actions):
    try:
        seconds = _nonnegative_number(seconds)
    except ValueError:
        return None
    if type(actions) is not int or not 0 <= actions <= MAX_EXACT_COUNT:
        return None
    return seconds < 1200 and actions < 60


def event_metrics(receipt):
    """Derive the window from start through the first useful-result event."""
    events = receipt.get('events') if isinstance(receipt, dict) else None
    if not isinstance(events, list) or len(events) < 2:
        raise ValueError('Missing event endpoints')
    start, previous, actions = None, None, 0
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get('kind') not in ('start', 'action', 'note', 'useful_result'):
            raise ValueError('Invalid event shape')
        at = event.get('at_s')
        _nonnegative_number(at)
        if previous is not None and at < previous:
            raise ValueError('Events are not ordered')
        previous = at
        if index == 0:
            if event['kind'] != 'start':
                raise ValueError('Missing start event')
            start = at
        elif event['kind'] == 'start':
            raise ValueError('Repeated start event')
        if event['kind'] == 'action':
            if type(event.get('deliberate')) is not bool:
                raise ValueError('Action intent is missing')
            actions += int(event['deliberate'])
        if event['kind'] == 'useful_result':
            return {'elapsed_s': at - start, 'deliberate_actions': actions}
    raise ValueError('Missing useful-result event')


def event_receipt_verdict(receipt):
    try:
        metrics = event_metrics(receipt)
    except ValueError:
        return 'UNREADABLE'
    within = budget_ok(metrics['elapsed_s'], metrics['deliberate_actions'])
    return 'UNREADABLE' if within is None else ('PASS' if within else 'FAIL')


def assemble_offline_evidence(bundle, drawings, *, recorded_grade=None):
    """Pure offline report; never writes a grade, readiness marker or live state."""
    bundle = bundle if isinstance(bundle, dict) else {}
    drawings = drawings if isinstance(drawings, dict) else {}
    first, second = drawings.get('first', {}), drawings.get('second', {})
    first = first if isinstance(first, dict) else {}
    second = second if isinstance(second, dict) else {}
    first_fixture = {'id': 'first', 'sha256': first.get('sha256')}
    second_fixture = {'id': 'second', 'sha256': second.get('sha256')}
    chain = bundle.get('publication')
    chain = chain if isinstance(chain, dict) else {}
    table = bundle.get('table')
    table = table if isinstance(table, dict) else {}
    def second_check():
        verdict = second_run_verdict(bundle.get('first_run'), bundle.get('second_run'), second_fixture, second)
        if verdict != 'PASS':
            return verdict
        published = chain.get('published')
        if (not isinstance(published, dict) or published.get('status') != 'published'
                or not _text(published.get('tool_id'))):
            return 'UNREADABLE'
        return 'PASS' if bundle['first_run']['published_tool_id'] == published['tool_id'] else 'FAIL'
    checks = [
        lambda: intake_verdict(bundle.get('intake'), first_fixture),
        lambda: publication_verdict(chain),
        lambda: schedule_verdict(table, first),
        lambda: spreadsheet_verdict(bundle.get('spreadsheet'), table.get('display_cells')),
        second_check,
        lambda: provenance_verdict(bundle.get('provenance'), chain.get('published'), chain.get('run')),
        lambda: event_receipt_verdict(bundle.get('timing')),
    ]
    prior = recorded_grade if isinstance(recorded_grade, dict) else {}
    prior_steps = prior.get('steps', [])
    previous = {}
    if isinstance(prior_steps, list):
        for row in prior_steps:
            if isinstance(row, dict) and type(row.get('n')) is int:
                n, value = row['n'], row.get('verdict')
                old = previous.get(n)
                previous[n] = ('FAIL' if 'FAIL' in (old, value) else
                               ('UNREADABLE' if old == 'UNREADABLE' or value != 'PASS' else value))
    steps = []
    for n, check in enumerate(checks, 1):
        try:
            verdict = check()
        except (ValueError, TypeError, KeyError, OverflowError):
            verdict = 'UNREADABLE'
        if recorded_grade is not None:
            old = previous.get(n, 'UNREADABLE')
            if old == 'FAIL' or (old != 'PASS' and verdict == 'PASS'):
                verdict = 'FAIL' if old == 'FAIL' else 'UNREADABLE'
        steps.append(Step(n, verdict, 'offline normalized evidence only',
                          expected='Frozen X06 step ' + str(n)))
    verdict = aggregate(steps)
    if prior.get('verdict') == 'FAIL':
        verdict = 'FAIL'
    elif recorded_grade is not None and prior.get('verdict') != 'PASS' and verdict == 'PASS':
        verdict = 'UNREADABLE'
    return {'schema': 'leaf.xbench.offline-evidence.v1', 'verdict': verdict, 'steps': steps}


def unreadable_steps(evidence, observed):
    return [Step(n, 'UNREADABLE', evidence, observed=observed,
                 expected='Frozen X06 acceptance step ' + str(n)) for n in range(1, 8)]


def author_verdict(status, body):
    """A named reachable product defect differs from unavailable evidence/auth."""
    if (type(status) is int and status == 503 and isinstance(body, dict)
            and body.get('reason_code') == 'tenant_repository_unavailable'
            and isinstance(body.get('error'), dict)
            and body['error'].get('error_code') == 'BAD_PARAMS'):
        return 'FAIL'
    return 'UNREADABLE'


@contextmanager
def author_window(path):
    """Serialize the shared staging tenant with the coordinator's atomic rail."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = json.dumps({'bench': 'X06', 'instance': uuid4().hex,
                        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()})
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(value)
        yield
    finally:
        if path.is_file() and path.read_text(encoding='utf-8') == value:
            path.unlink()


def run(ctx):
    path = Path('D:/agents/production-lanes-2026-11-10/XBENCH/driver/author-window.lock')
    try:
        with author_window(path):
            return _run(ctx)
    except FileExistsError:
        return unreadable_steps(str(path), 'Shared author window is owned; no new author request issued')


def _run(ctx):
    """Collect only observed normal UI facts, never bypass a provider grant."""
    from xbench_session import signed_in, redact

    # The manifest pins absolute fixture paths. The oracle is beside its actual
    # fixture manifest, not in the source repository or the product response.
    fixture_manifest_dir = Path('D:/agent-data/leaf-research/studio-xbench-2026-11/manifests')
    options = getattr(ctx, 'options', {})
    oracle_path = Path(options.get('oracle', fixture_manifest_dir / 'X06.oracle.json'))
    load_oracle(oracle_path, ctx.fixtures)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    evidence = ctx.out_dir / 'author-supply.json'
    observed = {'source_sha': ctx.source_sha, 'author_request': REQUEST,
                'scope': 'first authoring failure; unobserved steps unreadable',
                'browser_closed': False}
    verdict = 'UNREADABLE'
    repo = Path(__file__).resolve().parents[3]
    try:
        with signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, info):
            observed['web_build'] = info.get('web_build')
            welcome = page.get_by_role('button', name='Continue workspace', exact=True)
            if welcome.is_visible():
                welcome.click()
            # Continue only the lane-owned project. Never create, delete or
            # change another project's material to manufacture a fixture.
            page.get_by_role('button', name=re.compile(r'^Projects: change project')).click()
            page.get_by_role('menuitem', name=re.compile(r'^XBench X06(?:\s|$)')).first.click()
            chooser = page.get_by_role('button', name='Open project or material', exact=True)
            if chooser.is_visible():
                chooser.click()
            else:
                page.get_by_role('button', name='Project', exact=True).click()
            page.get_by_role('button', name='Material', exact=True).click()
            first = next(f for f in ctx.fixtures if f['id'] == 'first')
            filename = Path(first['path']).name
            page.get_by_text(filename, exact=True).first.wait_for(state='visible', timeout=30_000)
            preview = page.get_by_role('button', name='Preview v1 in the drawing', exact=True)
            preview.wait_for(state='visible', timeout=30_000)
            preview.click()
            # The normal version preview mounts the imported drawing. Record
            # its visible identity before opening authoring.
            page.get_by_text(filename, exact=True).first.wait_for(state='visible', timeout=30_000)
            page.screenshot(path=str(ctx.out_dir / 'imported-version.png'), full_page=True)
            observed['project_name'] = 'XBench X06'
            observed['fixture_sha256'] = first['sha256']
            # Open the normal Studio tools surface. No API author submission,
            # init script, internal state mutation, grant or credential read.
            studio = page.get_by_role('button', name='Studio', exact=True)
            if studio.is_visible():
                studio.click()
            author = page.get_by_role('button', name=re.compile(r'^Author a tool'))
            author.wait_for(state='visible', timeout=30_000)
            if author.get_attribute('aria-expanded') != 'true':
                author.click()
            field = page.get_by_label('What should the tool do?', exact=True)
            field.wait_for(state='visible', timeout=30_000)
            if field.is_visible():
                field.fill(REQUEST)
                observed['request_entered_in_normal_author_ui'] = True
                observed['generate_enabled'] = page.get_by_role(
                    'button', name='Generate tool', exact=True).is_enabled()
                if observed['generate_enabled']:
                    with page.expect_response(lambda r: '/api/author/stage' in r.url
                                              and r.request.method == 'POST', timeout=60_000) as staged:
                        page.get_by_role('button', name='Generate tool', exact=True).click()
                    response = staged.value
                    observed['author_http_status'] = response.status
                    body = response.json()
                    # The author error envelope contains no auth header or
                    # credential material. Never capture request headers.
                    if isinstance(body, dict) and isinstance(body.get('error'), dict):
                        observed['author_response'] = json.loads(redact(json.dumps(body)))
                    verdict = author_verdict(response.status, body)
                    if verdict == 'FAIL':
                        page.get_by_text(re.compile('Protected tool authoring could not complete')).wait_for(
                            state='visible', timeout=30_000)
            else:
                observed['request_entered_in_normal_author_ui'] = False
            text = redact(page.locator('body').inner_text())
            observed['linked_account_hint'] = (False if 'not linked' in text.lower()
                                                else None)
            (ctx.out_dir / 'author-supply.txt').write_text(text, encoding='utf-8')
            page.screenshot(path=str(ctx.out_dir / 'author-supply.png'), full_page=True)
            observed['blocker'] = ('tenant_repository_unavailable prevents protected authoring'
                                   if verdict == 'FAIL' else 'Authoring continuation evidence unavailable')
            observed['owner'] = 'e3cdcd02' if verdict == 'FAIL' else 'X06'
    except Exception as exc:
        observed['blocker'] = ('tenant_repository_unavailable prevents protected authoring'
                               if verdict == 'FAIL' else 'Supported staging UI or sign-in unreadable')
        observed['owner'] = 'e3cdcd02' if verdict == 'FAIL' else 'X06'
        observed['error'] = redact(str(exc))[:1000]
    finally:
        # signed_in closes its dedicated browser in its own finally, including
        # failures entering the context. No user or peer browser is touched.
        observed['browser_closed'] = True
        write_json_atomic(evidence, observed)
    steps = unreadable_steps(str(evidence), observed)
    if verdict == 'FAIL':
        steps[1] = Step(2, 'FAIL', str(evidence), observed=observed,
                        expected='Studio stages, reviews, publishes and runs the requested tool')
    return steps
