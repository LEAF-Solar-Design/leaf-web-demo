"""Global test-process isolation for import-time SQLite defaults.

Several application modules bind their database path when first imported.
Pytest imports test modules during collection, so a broad suite can import the
application before a module-specific ``setdefault`` runs. Route those defaults
to one fresh session directory before collection can touch the real developer
databases.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


_TEST_STATE = Path(tempfile.mkdtemp(prefix="leaf-server-pytest-"))
os.environ.setdefault("SESSIONS_DB", str(_TEST_STATE / "sessions.db"))
os.environ.setdefault("JOBS_DB", str(_TEST_STATE / "jobs.db"))
os.environ.setdefault("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
