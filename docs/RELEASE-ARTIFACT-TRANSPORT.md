# Release archive integrity

Parent: Studio source-to-staging artifact transport recovery.

`scripts/release_artifact_transport.py` is an offline content-verification
component. It checks an archive SHA256, exact regular-file member set, each
member size and digest, and compressed and expanded size bounds. It reads a
bounded snapshot and never extracts files or calls AWS or GitHub.

The caller must obtain expectations through independent, accepted producer
evidence and bind them to repository, source, build/run, attempt and immutable
object version. The returned `leaf.archive-integrity.v1` result does **not**
prove producer identity, test success, or deployment readiness. Local ZIP unit
fixtures are not fabricated positive provider receipts.

This component is not yet called by release workflows. It introduces no wire
contract or enabled deployment gate. Existing GitHub artifacts and deployed
releases remain unchanged. S3 upload/download, exact object descriptors,
provider-authority verification and cross-repository consumers remain to be
integrated. IAM and storage activation require their own resolved authority.

Only regular-file entries are supported in this first slice. Names must be
literal relative POSIX paths with no empty, dot, parent, control-character,
backslash or colon segments. Symlinks, directories, encrypted entries,
duplicates and unexpected members are refused. Consumers retain their own
payload-specific checks, such as the existing canonical web archive contract.

Run the focused tests from PowerShell:

```powershell
Set-Location C:/tmp/leaf-studio-artifact-verifier-20260906/scripts
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest test_release_artifact_transport.py test_platform_release_manifest.py -q
```

The command-local plugin setting avoids unrelated user-installed pytest
plugins. Running from `scripts` avoids the repository's `platform` package
shadowing Python's standard library during plugin initialization.
