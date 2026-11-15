"""X07 initial discovery blocker, through normal Studio routes and controls.

This slice can establish a missing first-choice proposal. It cannot certify the
remaining customer-profile workflow, and therefore cannot produce PASS.
"""

import csv
import json
from pathlib import Path
import re
from uuid import uuid4

from xbench_core import Step, read_health, write_json_atomic


LIMITS = ("Initial blocker slice: observes the first drawing's visible module proposal. "
          "Confirmation, competing-set selection, customer-profile reuse, isolation and "
          "version retention are untested and remain UNREADABLE. This slice never PASSes.")


def independent_counts(manifest):
    """Recount pinned discovery inputs, never the product's extracted output."""
    fixtures = {f['id']: f for f in manifest['fixtures']}
    result = {}
    for fixture in fixtures.values():
        if fixture.get('role') != 'drawing':
            continue
        oracle = fixture.get('count_oracle') or fixture.get('window_oracle')
        if not isinstance(oracle, dict):
            raise ValueError('independent count oracle missing')
        source = (fixtures[oracle['fixture_id']]['path'] if 'fixture_id' in oracle
                  else oracle['path'])
        if source not in {f['path'] for f in fixtures.values() if f.get('role') == 'oracle'}:
            raise ValueError('count oracle is not pinned as a fixture')
        window = fixture.get('design_window')
        count = 0
        with Path(source).open(newline='', encoding='utf-8-sig') as stream:
            for row in csv.reader(stream):
                if len(row) < 6 or row[0] != 'A':
                    continue
                if (row[1], row[3]) != (fixture['module_block'], fixture['module_layer']):
                    continue
                if window is not None:
                    x, y = float(row[4]), float(row[5])
                    if not (window[0] <= x <= window[2] and window[1] <= y <= window[3]):
                        continue
                count += 1
        if count != fixture['modules'] or count != oracle['expected_count']:
            raise ValueError('independent oracle count differs from frozen count')
        result[fixture['id']] = count
    if len(result) < 5:
        raise ValueError('five drawing oracles required')
    return result


def first_choice_step(text, fixture, *, selected, source_matches, evidence):
    expected = {'module_block': fixture['module_block'], 'module_layer': fixture['module_layer']}
    if not selected or not source_matches:
        return Step(1, 'UNREADABLE', evidence, 'drawing selection or source not established', expected)
    missing = [key for key, value in expected.items() if value.casefold() not in text.casefold()]
    if missing:
        return Step(1, 'FAIL', evidence,
                    {'blocker': 'ROLE_PROPOSAL_NOT_VISIBLE', 'missing_first_choice_names': missing}, expected)
    return Step(1, 'UNREADABLE', evidence,
                'Names are visible; first-choice semantics and confirmation still need grading', expected)


def open_version(page, project, version):
    page.reload()
    page.get_by_role('button', name=re.compile('^Projects: change project')).click(timeout=30000)
    page.get_by_role('menuitem', name=re.compile(re.escape(project['name']))).first.click(timeout=30000)

    def profile(value, *, show_project):
        page.get_by_role('combobox', name='Workspace profile', exact=True).select_option(value)
        page.wait_for_timeout(1000)
        if not page.locator('.environment-chooser[aria-hidden="true"]').count():
            page.get_by_role('button', name='Open project or material', exact=True).click(timeout=15000)
        sidebar = page.get_by_role('region', name='Project sidebar', exact=True)
        if show_project and not sidebar.is_visible():
            page.get_by_role('button', name='Project', exact=True).click(timeout=15000)
        if not show_project and sidebar.is_visible():
            page.get_by_role('button', name='Close Project', exact=True).click(timeout=15000)

    profile('browser', show_project=True)
    page.locator('[data-action="version"][data-id="' + version['version_id'] + '"]').click(timeout=30000)
    with page.expect_response(lambda r: version['version_id'] in r.url and '/context?' in r.url,
                              timeout=60000) as response:
        page.get_by_role('button', name=re.compile(re.escape(version['version_id']))).click(timeout=30000)
    if response.value.status != 200:
        raise ValueError('selected version context unavailable')
    body = response.value.json()
    binding = body.get('context', {})
    if body.get('degraded_mode') is not False or any(binding.get(key) != value for key, value in (
            ('project_id', project['project_id']), ('drawing_id', version['drawing_id']),
            ('input_version_id', version['version_id']))):
        raise ValueError('selected version context mismatch')
    profile('solar', show_project=False)
    page.get_by_text('Select a valid project drawing version to use Solar.', exact=True).wait_for(
        state='hidden', timeout=30000)
    page.wait_for_timeout(1500)


def run(ctx):
    import xbench_session
    from xbench_studio import StudioClient, disposable_project

    counts = independent_counts(ctx.manifest)
    fixture = next(f for f in ctx.manifest['fixtures'] if f.get('role') == 'drawing')
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    evidence = ctx.out_dir / 'discovery.json'
    steps = [Step(n, 'UNREADABLE', str(evidence), 'not reached by initial blocker slice')
             for n in range(1, 8)]
    observation = {'source_sha': ctx.source_sha, 'independent_counts': counts,
                   'cleanup': {'browser': 'pending', 'project': 'pending'}}
    try:
        with xbench_session.signed_in(Path(__file__).resolve().parents[3], ctx.source_sha,
                                      origin=ctx.base) as (page, _):
            def transport(method, path, **kw):
                status, _, raw = xbench_session.api(page, method, path, origin=ctx.base, **kw)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw
                return status, body

            client = StudioClient(transport)
            with disposable_project(client, 'X07-discovery-' + uuid4().hex[:12]) as project:
                drawing = client.upload_drawing('fixture.dwg', Path(fixture['path']).read_bytes())
                client.wait_upload(drawing, timeout_s=300, poll_s=5)
                version = client.adopt_drawing(project['project_id'], drawing, 'Discovery fixture')
                open_version(page, project, version)
                text = page.locator('body').inner_text()
                (ctx.out_dir / 'workspace.txt').write_text(text, encoding='utf-8')
                page.screenshot(path=str(ctx.out_dir / 'workspace.png'), full_page=False)
                matches = read_health(ctx.base) == ctx.source_sha
                observation.update(selected_version=version['version_id'], source_matches=matches)
                steps[0] = first_choice_step(text, fixture, selected=True, source_matches=matches,
                                             evidence=str(evidence))
            observation['cleanup']['project'] = 'deleted'
        observation['cleanup']['browser'] = 'closed'
    except Exception as exc:
        # Selector, auth, transport and cleanup failures are never product FAIL.
        observation['error_type'] = type(exc).__name__
        steps[0] = Step(1, 'UNREADABLE', str(evidence), 'probe or cleanup unavailable')
    write_json_atomic(evidence, observation)
    return steps
