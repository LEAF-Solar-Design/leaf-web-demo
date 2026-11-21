"""Static local-import closure, without importing or executing checked modules."""
import ast
from pathlib import Path
import tempfile
import unittest


class InventoryError(ValueError): pass


def closure(root, seeds, required):
    found, active = set(), set()
    def visit(path):
        if path in active: raise InventoryError('LOCAL_IMPORT_CYCLE')
        if path in found: return
        active.add(path)
        tree = ast.parse((root / path).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import): modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parent = Path(path).parent
                    for _ in range(node.level - 1): parent = parent.parent
                    candidates = [parent / ((node.module or '').replace('.', '/') + '.py')] if node.module else [parent / (item.name + '.py') for item in node.names]
                    for candidate in candidates:
                        if (root / candidate).is_file(): visit(candidate.as_posix())
                    continue
                modules = [node.module] if node.module else []
            for module in modules:
                for base in ['server', 'tools/xbench']:
                    candidate = (Path(base) / (module.replace('.', '/') + '.py')).as_posix()
                    if (root / candidate).is_file(): visit(candidate); break
        active.remove(path); found.add(path)
    for seed in seeds: visit(seed)
    if not found.issubset(required): raise InventoryError('INCOMPLETE_SOURCE_INVENTORY')
    return found


class SourceInventoryClosure(unittest.TestCase):
    def test_actual_journey_transitive_local_imports_are_in_fixed_inventory(self):
        root = Path(__file__).resolve().parents[3]
        tree = ast.parse((root / 'tools/xbench/x07_offline_acceptance_report.py').read_text())
        value = next(node.value.args[0] for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'REQUIRED_SOURCE_FILES' for target in node.targets))
        required = ast.literal_eval(value)
        found = closure(root, ['tools/xbench/x07_synthetic_journey.py', 'tools/xbench/x07_offline_acceptance_report.py'], required)
        self.assertEqual(found, required)

    def test_omitted_transitive_dependency_refuses_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'server').mkdir()
            for name, text in {'x07_a': 'import x07_b\nraise RuntimeError("never execute")',
                               'x07_b': 'from x07_c import value', 'x07_c': 'value=1'}.items():
                (root / f'server/{name}.py').write_text(text)
            required = {'server/x07_a.py', 'server/x07_b.py'}
            with self.assertRaisesRegex(InventoryError, '^INCOMPLETE_SOURCE_INVENTORY$'):
                closure(root, ['server/x07_a.py'], required)
            required.add('server/x07_c.py')
            self.assertEqual(closure(root, ['server/x07_a.py'], required), required)
            self.assertFalse((root / 'server/__pycache__').exists())

    def test_relative_imports_are_followed_statically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'server').mkdir()
            (root / 'server/a.py').write_text('from .b import value')
            (root / 'server/b.py').write_text('value=1')
            required = {'server/a.py', 'server/b.py'}
            self.assertEqual(closure(root, ['server/a.py'], required), required)

    def test_local_cycle_refuses_without_recursion_or_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'server').mkdir()
            (root / 'server/x07_a.py').write_text('import x07_b')
            (root / 'server/x07_b.py').write_text('import x07_a')
            with self.assertRaisesRegex(InventoryError, '^LOCAL_IMPORT_CYCLE$'):
                closure(root, ['server/x07_a.py'], {'server/x07_a.py', 'server/x07_b.py'})
