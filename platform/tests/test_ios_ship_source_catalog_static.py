"""Migration contract for W4h S1 source discovery."""
from pathlib import Path
import re


# S1 row12
def test_source_catalog_migration_is_next_and_immutable():
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    path = migrations / "0065_ios_ship_source_catalog.sql"
    sql = path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ios_ship_source_catalog" in sql
    assert "UNIQUE (org_id, project_id, source_revision)" in sql
    for name in ("source_sha256", "producer_receipt_digest"):
        assert f"CHECK ({name} ~ '^[0-9a-f]{{64}}$')" in sql
    assert "BEFORE UPDATE OR DELETE ON ios_ship_source_catalog" in sql
    assert "FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation()" in sql
    assert "REFERENCES projects(org_id, project_id)" in sql
    previous = [int(p.name[:4]) for p in migrations.glob("*.sql")
                if re.match(r"\d{4}_", p.name) and int(p.name[:4]) < 65]
    assert max(previous) + 1 == int(path.name[:4]) == 65
    for name in ("catalog_key", "repository", "source_revision", "source_sha256",
                 "bundle_identifier", "marketing_version", "build_number", "producer_receipt_digest"):
        assert f"char_length(btrim({name})) > 0" in sql
