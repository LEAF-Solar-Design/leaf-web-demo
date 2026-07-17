"""Leaf web-CAD platform — canonical Project/Job persistence entity.

This package's directory is named ``platform/``, which shadows Python's stdlib
``platform`` module whenever the repo root sits on ``sys.path``. To keep the
stdlib importable, THIS PACKAGE NEVER imports the top-level name ``platform`` and
uses only *relative* imports internally (``from .db import ...``). Tests and the
integration host import this package under a non-colliding alias (``leaf_platform``)
or mount ``api.router`` directly. See platform/README.md for the full rationale.

Public surface:
    - models:   Org, Project, DrawingVersion, Job, BuiltTool dataclasses
    - db:       connection pool + cursor helpers (reads DATABASE_URL)
    - store:    org-scoped reads/writes (every read binds an org_id predicate)
    - offboard: offboard_org() — the ONLY hard-delete path (compliance exception)
    - api:      FastAPI APIRouter (create/list/open projects + jobs)
    - deps:     get_org_id dependency (dev: X-Org-Id header; prod seam for auth sibling)
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
