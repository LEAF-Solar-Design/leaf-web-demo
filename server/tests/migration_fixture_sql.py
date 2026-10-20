"""Bounded test-only relocation of 0079's explicitly owned public objects."""
from __future__ import annotations

import re


_MIGRATION = "0079_integration_turn_grants.sql"
_SCHEMA = re.compile(r"demo_test_([0-9a-f]{32})")
_PRODUCTION_ROLE = "leaf_integration_grant_app"


def fixture_grant_role(schema: str) -> str:
    """Return a unique PostgreSQL role below its 63-byte identifier limit."""
    match = _SCHEMA.fullmatch(schema)
    if match is None:
        raise ValueError("expected a demo_test_ schema with a 32-hex suffix")
    return "demo_grant_" + match[1]


def translate_migration_sql(name: str, text: str, schema: str) -> str:
    """Preserve every byte except the known schema/role tokens in 0079.

    Exact occurrence counts deliberately pin this small mapping to the shipped
    migration. SQL drift requires explicit review of the fixture translation.
    This helper never connects to a database or reads environment settings.
    """
    role = fixture_grant_role(schema)
    if name != _MIGRATION:
        return text
    replacements = (
        ("public.integration_turn_grants", schema + ".integration_turn_grants", 8),
        ("public.guard_integration_turn_grant", schema + ".guard_integration_turn_grant", 2),
        ("GRANT USAGE ON SCHEMA public", "GRANT USAGE ON SCHEMA " + schema, 1),
        (_PRODUCTION_ROLE, role, 6),
    )
    for old, new, expected in replacements:
        pattern = re.compile(r"(?<![A-Za-z_0-9$])" + re.escape(old)
                             + r"(?![A-Za-z_0-9$])")
        text, count = pattern.subn(lambda match: new, text)
        if count != expected:
            raise ValueError(f"{_MIGRATION}: expected {expected} occurrences of {old}, got {count}")
    if re.search(r'(?i)(?:\bpublic|"public")\s*\.', text) or _PRODUCTION_ROLE in text:
        raise ValueError(f"{_MIGRATION}: unmapped production schema or role")
    return text
