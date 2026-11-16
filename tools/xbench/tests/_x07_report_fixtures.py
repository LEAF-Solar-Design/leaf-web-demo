"""Load committed X07 test fixtures without depending on pytest path order."""
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch


def load(name, filename):
    path = Path(__file__).resolve().parents[3] / 'server' / 'tests' / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load('_x07_committed_report_fixture', 'test_x07_offline_acceptance_report.py')
with patch.dict(sys.modules, {'test_x07_offline_acceptance_report': fixtures}):
    cli = load('_x07_committed_report_cli_fixture', 'test_x07_report_cli_integrity.py')
