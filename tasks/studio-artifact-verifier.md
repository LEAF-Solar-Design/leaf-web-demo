# Studio artifact verifier slice

Parent: AWS-native release transport recovery for Studio1090.

- Scope: new offline verifier, focused tests and usage documentation only.
- Preserve: frozen4b source and all existing workflow, IAM and storage behavior.
- Baseline: existing release-manifest suite, with unrelated pytest plugins off.
- Acceptance: bounded snapshot, exact digest/member equality, no extraction,
  unsafe-entry rejection, and an explicit content-only result.
- Risk boundary: content integrity must never be promoted to producer authority.
- Integration remaining: transport descriptor, trusted producer admission,
  S3 retrieval/publication, workflow wiring and cross-repo consumers.

## Verified source receipt

- Combined focused tests: 110 passed, one existing duplicate-ZIP warning.
- Command: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest test_release_artifact_transport.py test_platform_release_manifest.py -q`
- Run directory: repository `scripts` directory. Exit code: 0.
- Self-review: no existing source, workflow, IAM or storage files changed.
- Publication is not performed. No provider build or production action ran.
- Local rollback: revert the source commit. No live state needs rollback.
