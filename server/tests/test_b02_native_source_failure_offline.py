"""Portable Native serializer/middleware units with real function AST nodes."""
import ast
import asyncio
import inspect
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
import uuid

SERVER = Path(__file__).resolve().parents[1]


def nodes(filename, names, namespace):
    tree = ast.parse((SERVER / filename).read_text())
    selected = [node for node in tree.body if getattr(node, 'name', None) in names
                or isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    exec(compile(ast.Module(body=selected, type_ignores=[]), filename, 'exec'), namespace)
    return namespace


def fixture():
    flow = nodes('browser_project_flow.py', {'_ERRORS', 'FlowError', '_author_failure'}, {'uuid': uuid})
    response = lambda value, status=200: SimpleNamespace(body=value, status_code=status,
                                                        headers={'Cache-Control': 'private, no-store'})
    native = nodes('routers/native_project.py', {'_PATH', '_LOCAL_REFUSAL', '_LOCAL_INITIALIZER',
                   '_failure', 'no_store_responses'}, {'re': re, 'flow': SimpleNamespace(**flow),
                                                        '_response': response})
    request = SimpleNamespace(scope={}, url=SimpleNamespace(path='/api/projects/fixture/native/source/requests'))
    return flow, native, request


def send(native, request, error):
    async def call_next(current):
        function = native['_failure']
        return function(error, current) if 'request' in inspect.signature(function).parameters else function(error)
    return asyncio.run(native['no_store_responses'](request, call_next))


class NativeSourceFailure(unittest.TestCase):
    def test_actual_producer_metadata_survives_native_serializer_and_middleware(self):
        flow, native, request = fixture()
        correlation = str(uuid.uuid4())
        error = flow['_author_failure'](503, 'author_unavailable', 'stage', correlation,
            '11111111-1111-4111-8111-111111111111', {'author_subphase': 'initialize_http', 'author_http_status': 503})
        response = send(native, request, error)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.body['error'], dict(code=error.code, message=str(error), **error.public_diagnostic))
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_generic_error_and_spoofed_scope_do_not_expose_diagnostics(self):
        flow, native, request = fixture()
        request.scope['_native_source_initializer_failure'] = (object(), {'private': 'PRIVATE'})
        error = flow['FlowError'](503)
        error.public_diagnostic = {'private': 'PRIVATE'}
        response = send(native, request, error)
        self.assertEqual(response.body, {'error': {'code': error.code, 'message': str(error)}})

    def test_existing_pre_generation_refusal_provenance_is_preserved(self):
        flow, native, request = fixture()
        value = {'error': {'code': 'video_unavailable', 'message': 'Fixed refusal'}, 'refusal': {'stage': 'before_generation'}}
        request.scope['_native_source_pre_generation_refusal'] = (native['_LOCAL_REFUSAL'], value)
        response = send(native, request, flow['FlowError'](503))
        self.assertEqual(response.body, value)

    def test_unlisted_diagnostic_fields_and_private_message_are_not_copied(self):
        flow, native, request = fixture()
        error = flow['_author_failure'](503, 'author_unavailable', 'stage', str(uuid.uuid4()),
            '11111111-1111-4111-8111-111111111111', {'author_subphase': 'initialize_transport'})
        error.args = ('PRIVATE EXCEPTION',)
        error.public_diagnostic['secret'] = 'PRIVATE SECRET'
        response = send(native, request, error)
        self.assertNotIn('PRIVATE', str(response.body))
        self.assertNotIn('secret', response.body['error'])


if __name__ == '__main__':
    unittest.main()
