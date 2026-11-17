from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import test_x07_offline_acceptance_report as fixtures
import test_x07_report_cli_integrity as cli_helpers


class ReportOutputAliases(unittest.TestCase):
    def test_receipt_and_verified_source_aliases_refuse_before_replacement(self):
        for target in ['journey', 'proof', 'source']:
            for alias in ['normalized', 'hardlink']:
                with self.subTest(target=target, alias=alias), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                    selected = {'journey': paths[0], 'proof': paths[1], 'source': root / 'server/fixture.py'}[target]
                    before = {path: path.read_bytes() for path in [*paths, *(root / name for name in journey['files'])]}
                    if alias == 'hardlink':
                        output = root / 'alias.json'
                        os.link(selected, output)
                    else:
                        (selected.parent / 'nested').mkdir()
                        output = selected.parent / 'nested' / '..' / selected.name
                    with patch.object(fixtures.report.os, 'replace') as replacement:
                        code, value = cli_helpers.ReportCliIntegrity().call(root, paths, ['--out', str(output)])
                    self.assertEqual(code, 2)
                    self.assertEqual(value['error'], 'OUTPUT_ALIASES_INPUT_OR_SOURCE')
                    replacement.assert_not_called()
                    self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))
