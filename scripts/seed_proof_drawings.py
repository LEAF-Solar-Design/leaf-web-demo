"""Explicitly provision the two proof-mode drawings in an isolated local store."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def seed_proof_drawings(tenant: str) -> None:
    # Check before importing the store: never fall back to server/drawings or
    # create a misspelled/missing run root as a side effect of provisioning.
    root = os.environ.get("LEAF_STORE_DIR", "")
    if not root.strip() or not Path(root).is_dir():
        raise ValueError("LEAF_STORE_DIR must name an existing proof store directory")

    server = str(Path(__file__).resolve().parent.parent / "server")
    if server not in sys.path:
        sys.path.insert(0, server)
    import write_loop
    import store
    from tenant_id_validator import validate_tenant_id

    validate_tenant_id(tenant)
    backend = write_loop.default_backend()
    if not isinstance(backend, store.FilesystemBackend) or store.authority_mode() != "legacy":
        raise ValueError("proof drawing seeding requires local filesystem authority")
    for drawing_id in ("cat", "cat-panels"):
        if not backend.exists(store.manifest_key(tenant, drawing_id)):
            store.ingest_drawing(
                backend, tenant, str(write_loop.CACHED_INTAKE_PATH), drawing_id=drawing_id
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", action="append", required=True)
    args = parser.parse_args()
    for tenant in dict.fromkeys(args.tenant):
        seed_proof_drawings(tenant)


if __name__ == "__main__":
    main()
