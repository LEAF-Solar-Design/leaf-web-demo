"""Static guard for the deferred sizing-exchange function's execution rights."""
from pathlib import Path
import re


def test_sizing_check_security_definer_migration():
    migration = Path(__file__).parents[1] / 'migrations' / '0116_sizing_exchange_check_security_definer.sql'
    assert migration.is_file()
    text = migration.read_text(encoding='utf-8')
    assert text.splitlines()[0] == '-- expand-contract: expand-only'
    assert 'ALTER FUNCTION %I.check_project_sizing_exchange_completion() SECURITY DEFINER' in text
    assert 'SET search_path = %I, pg_catalog, pg_temp' in text
    assert 'REVOKE ALL ON FUNCTION %I.check_project_sizing_exchange_completion() FROM PUBLIC' in text
    assert 'pg_catalog.current_schema()' in text

    # The required rollback header mentions GRANT; guard SQL statements.
    statements = re.sub(r'--[^\n]*', '', text)
    assert not re.search(r'\bCREATE\s+OR\s+REPLACE\s+FUNCTION\b', statements, re.IGNORECASE)
    assert not re.search(r'\bDROP\b', statements, re.IGNORECASE)
    assert not re.search(r'\bGRANT\b', statements, re.IGNORECASE)
    assert not re.search(r'\bTRIGGER\b', statements, re.IGNORECASE)
