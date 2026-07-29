"""Apply instant execution control-plane PostgreSQL migrations once.

This module is intended for a one-shot ECS task. It uses the same production
configuration loader as the WSGI and reaper processes, so the database URL can
come from the mounted secret file and must still use ``sslmode=verify-full``.
"""
from __future__ import annotations

from pathlib import Path

from .production import ProductionConfigurationError, load_production_settings


MIGRATIONS = Path(__file__).with_name("migrations")


def apply_migrations(*, migrations_directory: Path = MIGRATIONS) -> list[str]:
    settings = load_production_settings()
    try:
        import psycopg
    except ImportError as exc:
        raise ProductionConfigurationError("psycopg is required for control-plane migrations") from exc
    files = sorted(migrations_directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise ProductionConfigurationError("no control-plane migrations were packaged")
    with psycopg.connect(settings.database_url) as connection:
        for migration in files:
            connection.execute(migration.read_text(encoding="utf-8"))
    return [migration.name for migration in files]


def main() -> None:
    applied = apply_migrations()
    print(f"applied {len(applied)} instant control-plane migrations")


if __name__ == "__main__":
    try:
        main()
    except ProductionConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
