"""Static guard for the deferred sizing-exchange function's execution rights."""
from pathlib import Path
import re


def test_sizing_check_security_definer_migration():
    migration = Path(__file__).parents[1] / 'migrations' / '0118_sizing_exchange_check_security_definer.sql'
    assert migration.is_file()
    text = migration.read_text(encoding='utf-8')
    assert text.splitlines()[0] == '-- expand-contract: expand-only'
    assert 'SECURITY DEFINER' in text
    assert 'SET search_path = pg_catalog, pg_temp' in text
    assert '%1$I.jobs' in text
    assert '%1$I.project_sizing_exchanges' in text
    body = text.split('$fn$')[1]
    assert not re.search(r'(?<![.$I])\bjobs\b', body)
    assert not re.search(r'(?<![.$I])\bproject_sizing_exchanges\b', body)
    assert "ERRCODE = '23514'" in body
    assert 'FOR UPDATE' in body
    assert 'REVOKE ALL ON FUNCTION %I.check_project_sizing_exchange_completion() FROM PUBLIC' in text
    assert 'pg_catalog.current_schema()' in text

    statements = re.sub(r'--[^\n]*', '', text)
    assert 'CREATE OR REPLACE FUNCTION %1$I.check_project_sizing_exchange_completion()' in statements
    assert not re.search(r'\bDROP\b', statements, re.IGNORECASE)
    assert not re.search(r'\bGRANT\b', statements, re.IGNORECASE)
    assert re.findall(r'\bTRIGGER\b', statements, re.IGNORECASE) == ['TRIGGER']
    assert 'RETURNS TRIGGER' in statements
