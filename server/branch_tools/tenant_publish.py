"""Publish reviewed, tenant-bound Branch tools using the online signer."""
from dataclasses import dataclass
from datetime import timezone
import io
import json
import threading
import zipfile

from branch_tools import online_signer, tenant_catalog
from branch_tools.package_check import check_branch_package

TENANT_TIMEOUT_CAP_MS = 60_000  # tenant tools may block AutoCAD's UI thread for at most 60 s


class TenantPublishError(RuntimeError):
    def __init__(self, code, status_code=503, failures=None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.failures = failures or []


@dataclass(frozen=True)
class TenantPublishResult:
    name: str
    version: str
    package_digest: str


# Serialize read/append/write revocations with refreshes in this worker.
_revocations_lock = threading.RLock()


def timestamp(now):
    return now.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def reviewed_package(files, name, *, timeout_cap_ms=TENANT_TIMEOUT_CAP_MS):
    checked = check_branch_package(files, name)
    capabilities = (checked.manifest or {}).get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    failures = [{"rule": "capability_not_allowed", "path": "tool.json",
                 "message": f"capability not allowed: {capability}"}
                for capability in capabilities if isinstance(capability, str)
                and capability.split(":", 1)[0] in {"files.write", "net.http"}]
    if failures:
        raise TenantPublishError("capability_not_allowed", 422, failures)
    if not checked.ok:
        raise TenantPublishError("branch_package_invalid", 422, checked.failures)
    manifest = dict(checked.manifest)
    manifest["review"] = {"status": "reviewed"}
    timeout = manifest.get("timeout_ms")
    if (timeout_cap_ms is not None and isinstance(timeout, int) and not isinstance(timeout, bool)
            and timeout > timeout_cap_ms):
        manifest["timeout_ms"] = timeout_cap_ms
    files = dict(files)
    files["tool.json"] = online_signer.canonical_json(manifest)
    checked = check_branch_package(files, name)
    if not checked.ok:
        raise TenantPublishError("branch_package_invalid", 422, checked.failures)
    return files, checked


def _entries(root, tenant_id):
    raw = tenant_catalog.revocations_bytes(root, tenant_id)
    if raw is None:
        # An invalid stored list must not be replaced with an empty one.
        directory = tenant_catalog._tenant(root, tenant_id)
        if (directory / "revocations.json").exists():
            raise TenantPublishError("tenant_catalog_unavailable")
        return []
    return json.loads(raw)["entries"]


def refresh_revocations(root, tenant_id, signer, now, *, entries=None):
    if signer is None:
        raise TenantPublishError("signer_unavailable")
    with _revocations_lock:
        if entries is None:
            entries = _entries(root, tenant_id)
        try:
            raw = online_signer.sign_revocations(signer, entries=entries, issued_at=timestamp(now))
        except online_signer.SignerUnavailable as exc:
            raise TenantPublishError("signer_unavailable") from exc
        tenant_catalog.write_revocations(root, tenant_id, raw)
    return raw


def read_publication(root, tenant_id, name, version):
    # Include revoked versions: publication remains immutable after revocation.
    entry = tenant_catalog._entry(root, tenant_id, name, version, set())
    if entry is None:
        raise TenantPublishError("branch_version_conflict", 409)
    return TenantPublishResult(name, version, entry[0]["package"]["package_digest"])


def publish_tenant_tool(files, name, tenant_id, signer, root, now):
    if signer is None:
        raise TenantPublishError("signer_unavailable")
    files, checked = reviewed_package(files, name)
    version, digest = checked.manifest["version"], checked.package_digest
    destination = tenant_catalog._version(root, tenant_id, name, version)
    if destination.exists():
        result = read_publication(root, tenant_id, name, version)
        if result.package_digest != digest:
            raise TenantPublishError("branch_version_conflict", 409)
        refresh_revocations(root, tenant_id, signer, now)
        return result
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(f"{name}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    try:
        envelope = online_signer.sign_envelope_v2(
            signer, name=name, version=version, package_digest=digest, tiers=["self_hosted", "studio_free"],
            audience_tenant=tenant_id, signed_at=timestamp(now))
    except online_signer.SignerUnavailable as exc:
        raise TenantPublishError("signer_unavailable") from exc
    # Both signatures succeed before exposing a package.
    refresh_revocations(root, tenant_id, signer, now)
    try:
        tenant_catalog.write_version(root, tenant_id, name, version, package.getvalue(), envelope, digest)
    except tenant_catalog.TenantCatalogError as exc:
        if exc.code != "version_exists":
            raise
        result = read_publication(root, tenant_id, name, version)
        if result.package_digest != digest:
            raise TenantPublishError("branch_version_conflict", 409) from exc
        return result
    return TenantPublishResult(name, version, digest)


def revoke_tenant_tool(root, tenant_id, name, version, reason, signer, now):
    result = read_publication(root, tenant_id, name, version)
    if signer is None:
        raise TenantPublishError("signer_unavailable")
    with _revocations_lock:
        entries = _entries(root, tenant_id)
        if not any(item["package_digest"] == result.package_digest for item in entries):
            entries.append({"name": name, "version": version, "package_digest": result.package_digest,
                            "reason": reason, "revoked_at": timestamp(now)})
        refresh_revocations(root, tenant_id, signer, now, entries=entries)
    return result
