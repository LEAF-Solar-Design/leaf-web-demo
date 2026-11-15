"""Portable branch-unit witness. No app imports, credentials, network or lease.

Compile the actual initializer, author and mutate try/except/finally nodes with
inert collaborators. This is not a router integration or rendered UI test.
The public contract test also requires the serializer supplied by B03 PR3024.
"""
import ast
import copy
import hashlib
import json
import logging
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
import uuid


SERVER = Path(__file__).resolve().parents[1]
ORG = '11111111-1111-4111-8111-111111111111'
PROJECT = '22222222-2222-4222-8222-222222222222'
REPO = '33333333-3333-4333-8333-333333333333'


def load_nodes(filename, names, namespace):
    tree = ast.parse((SERVER / filename).read_text(encoding='utf-8'))
    selected = [node for node in tree.body if getattr(node, 'name', None) in names
                or isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    exec(compile(ast.Module(body=selected, type_ignores=[]), filename, 'exec'), namespace)
    return namespace


def mutate_error_branch(function):
    # A nested branch could omit a newly introduced enclosing admission guard.
    # Refuse that topology instead of silently testing a different entry path.
    candidates = [node for node in function.body if isinstance(node, ast.Try) and any(
        isinstance(handler.type, ast.Attribute) and handler.type.attr == 'AuthoringError'
        for handler in node.handlers)]
    if len(candidates) != 1:
        raise ValueError('Fixture requires exactly one top-level AuthoringError handler')
    return candidates[0]


def simulate(failure='http'):
    diagnostics = {}
    exec(compile((SERVER / 'project_author_diagnostics.py').read_text(),
                 'project_author_diagnostics.py', 'exec'), diagnostics)
    diagnostic = SimpleNamespace(**diagnostics)
    writes, events, finished, downstream = [], [], [], []
    class TransportError(Exception):
        pass
    def post(*args, **kwargs):
        if failure == 'transport':
            raise TransportError('PRIVATE fixture transport details')
        return SimpleNamespace(status_code=503)
    def authority(*args):
        if failure == 'authority':
            raise RuntimeError('PRIVATE fixture database details')
        return dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=REPO)
    requests = SimpleNamespace(post=post, RequestException=TransportError)
    source = load_nodes('project_repository_source.py', {
        'CONTRACT', '_AUTHORITY', '_RESPONSE', 'SourceUnavailable', 'SourceConflict',
        '_uuid', 'initialize_project_source'}, {
        're': re, 'uuid': uuid, 'json': json, 'hashlib': hashlib,
        'os': SimpleNamespace(environ={'LEAF_AUTHOR_HARNESS_URL': 'https://fixture.invalid'}),
        'requests': requests, 'author_diagnostics': diagnostic,
        'broker_client': SimpleNamespace(harness_headers=lambda: {} if failure == 'binding'
                                       else {'X-Harness-Secret': 'inert-synthetic'}),
        'platform_link': SimpleNamespace(platform_store=lambda: SimpleNamespace(
            ensure_project_repository_authority=authority, save_portfolio=lambda *args: writes.append(args)))})
    author = load_nodes('browser_project_authoring.py', {'_ERRORS', 'WEB_PROFILE', 'NATIVE_PROFILE',
                       'AuthoringError', 'author'}, {
        'time': SimpleNamespace(monotonic=lambda: 0),
        '_request': lambda value, **kwargs: value,
        '_scope': lambda *args: (ORG, PROJECT, 'fixture-actor'),
        '_materials': lambda *args, **kwargs: ([], None),
        '_repo': lambda *args: downstream.append('repository-after-initialize'),
        'source': SimpleNamespace(**source)})
    def stage(*args, **kwargs):
        result = author['author']('tenant', PROJECT, {
            'source_operation': 'create', 'requirements': 'Synthetic portfolio'})
        writes.append(result)  # A returned candidate is the only publish boundary in this fixture.
        return result
    flow = load_nodes('browser_project_flow.py', {'_ERRORS', 'FlowError', '_author_failure', 'VideoUnavailableBeforeGeneration'}, {'uuid': uuid})
    flow.update(authoring=SimpleNamespace(**author), author_diagnostics=diagnostic,
                logging=logging, json=json, __name__='b02_offline_fixture',
                action='create', phase='authority_recheck', failure_status=503,
                failure_code='service_unavailable', failure_metadata={},
                correlation_id=str(uuid.uuid4()), success=False, known_refusal=False,
                recheck=lambda: None, _stage=stage, tenant='tenant', project_id=PROJECT,
                current=(ORG, PROJECT), body={}, session_id='fixture-session', turn_id='fixture-turn',
                source_profile='web-next-static-v1', NATIVE_PROFILE='ios-swiftui-xcodegen-v1',
                edit_id='44444444-4444-4444-8444-444444444444', subject='fixture-subject',
                session_store=SimpleNamespace(append_event=lambda sid, turn, kind, data: events.append((kind, data))),
                turn_runner=SimpleNamespace(finish_stage_authority=lambda *args, **kwargs: finished.append(args)))
    tree = ast.parse((SERVER / 'browser_project_flow.py').read_text())
    mutate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'mutate')
    branch = mutate_error_branch(mutate)
    wrapper = ast.parse('def run():\n    pass\n').body[0]
    # These flags are initialized by mutate before the extracted branch.
    wrapper.body = [ast.Global(names=['success', 'known_refusal']), branch]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 'browser_project_flow.py:error_branch', 'exec'), flow)
    flow['diagnostic_token'] = diagnostic.begin()
    try:
        flow['run']()
        raise AssertionError('Initializer failure did not fail closed')
    except flow['FlowError'] as error:
        caught = error
    router = load_nodes('routers/browser_project.py', {'_failure'}, {
        '_response': lambda value, status=200: {'status': status, 'body': value}})
    return SimpleNamespace(event=events[0][1], response=router['_failure'](caught), error=caught,
                           writes=writes, finished=finished, downstream=downstream,
                           context=diagnostic.snapshot())


class InitializerFailureOffline(unittest.TestCase):
    def test_fixture_refuses_nested_or_ambiguous_handler_topology(self):
        tree = ast.parse((SERVER / 'browser_project_flow.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'mutate')
        branch = mutate_error_branch(function)
        nested = copy.deepcopy(function)
        nested.body = [ast.If(test=ast.Constant(value=True), body=[copy.deepcopy(branch)], orelse=[])]
        ambiguous = copy.deepcopy(function)
        ambiguous.body.append(copy.deepcopy(branch))
        for changed in (nested, ambiguous):
            with self.assertRaisesRegex(ValueError, 'top-level AuthoringError handler'):
                mutate_error_branch(changed)

    def test_flow_error_retains_closed_diagnostic_before_shared_serializer(self):
        result = simulate()
        self.assertEqual(result.error.public_diagnostic, {
            'source_request_id': result.event['source_request_id'],
            'correlation_id': result.event['correlation_id'], 'phase': 'stage',
            'author_subphase': 'initialize_http', 'author_http_status': 503})
        self.assertEqual(result.error.code, 'author_unavailable')

    def test_noninitializer_and_malformed_diagnostics_stay_generic(self):
        ns = load_nodes('browser_project_flow.py', {'_ERRORS', 'FlowError', '_author_failure'}, {'uuid': uuid})
        valid = {'author_subphase': 'initialize_http', 'author_http_status': 503}
        args = [503, 'author_unavailable', 'stage', str(uuid.uuid4()), PROJECT, valid]
        for change in ({0: 409}, {1: 'fixture-private'}, {2: 'publish'}, {3: 'private'},
                       {4: 'private'}, {5: {**valid, 'private': 'private'}},
                       {5: {'author_subphase': 'author_http'}},
                       {5: {'author_subphase': ['private']}},
                       {5: {**valid, 'author_http_status': True}}):
            modified = [change.get(index, value) for index, value in enumerate(args)]
            error = ns['_author_failure'](*modified)
            self.assertEqual(error.public_diagnostic, {})
            self.assertNotIn('private', str(error))

    def test_http_failure_is_specific_in_retained_event(self):
        result = simulate()
        self.assertEqual(result.event['author_subphase'], 'initialize_http')
        self.assertEqual(result.event['author_http_status'], 503)
        self.assertEqual(result.event['code'], 'author_unavailable')
        self.assertEqual(str(uuid.UUID(result.event['correlation_id'])), result.event['correlation_id'])

    def test_failure_paths_do_not_return_or_write_partial_portfolio(self):
        for failure in ('http', 'transport', 'binding', 'authority'):
            with self.subTest(failure=failure):
                result = simulate(failure)
                self.assertEqual(result.writes, [])
                self.assertEqual(result.downstream, [])
                self.assertEqual(len(result.finished), 1)
                self.assertEqual(result.context, {})

    def test_private_details_do_not_escape(self):
        for failure in ('transport', 'authority'):
            result = simulate(failure)
            self.assertNotIn('PRIVATE', json.dumps([result.event, result.response]))

    def test_public_error_carries_specific_phase_and_same_correlation(self):
        result = simulate()
        public = result.response['body']['error']
        self.assertEqual(public.get('correlation_id'), result.event['correlation_id'])
        self.assertEqual(public.get('author_subphase'), 'initialize_http')
        self.assertEqual(public['code'], 'author_unavailable')


if __name__ == '__main__':
    unittest.main()
