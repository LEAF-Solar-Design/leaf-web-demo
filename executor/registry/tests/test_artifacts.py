from __future__ import annotations

import hashlib
import unittest

from executor.registry import ArtifactReference, ArtifactRegistryError, ImmutableArtifactRegistry, SignedArtifact
from executor.registry.artifacts import ImmutableArtifactRegistry as RegistryImplementation
from executor.runtime.ed25519 import public_key, sign


SEED = bytes(range(32))


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def reference(*, tenant_id: str = "tenant-demo", catalog_version: str = "sha256:" + "d" * 64,
              artifact_digest: str | None = None, code_digest: str | None = None) -> ArtifactReference:
    bytes_value = b"def run(intake, params):\n return {'ok': True}\n"
    return ArtifactReference(tenant_id, catalog_version, artifact_digest or digest(bytes_value), code_digest or digest(bytes_value))


def artifact(item: ArtifactReference, bytes_value: bytes = b"def run(intake, params):\n return {'ok': True}\n",
             signature: bytes | None = None) -> SignedArtifact:
    unsigned = SignedArtifact(item, bytes_value, "registry-key", b"")
    return SignedArtifact(item, bytes_value, "registry-key", signature or sign(SEED, RegistryImplementation._signed_payload(unsigned)))


class ImmutableArtifactRegistryTests(unittest.TestCase):
    def registry(self, item: ArtifactReference, *, bytes_value: bytes = b"def run(intake, params):\n return {'ok': True}\n",
                 signature: bytes | None = None, revoked: frozenset[str] = frozenset()) -> ImmutableArtifactRegistry:
        return ImmutableArtifactRegistry((artifact(item, bytes_value, signature),), {"registry-key": public_key(SEED)},
                                         revoked_artifact_digests=revoked)

    def test_resolves_verified_bytes_for_the_exact_identity(self) -> None:
        item = reference()
        registry = self.registry(item)
        self.assertEqual(b"def run(intake, params):\n return {'ok': True}\n", registry.resolve(item))

    def test_changed_bytes_fail_digest_verification(self) -> None:
        item = reference()
        registry = self.registry(item, bytes_value=b"changed bytes")
        with self.assertRaisesRegex(ArtifactRegistryError, "code digest") as raised:
            registry.resolve(item)
        self.assertEqual("CODE_DIGEST_MISMATCH", raised.exception.code)

    def test_wrong_signature_fails_closed(self) -> None:
        item = reference()
        registry = self.registry(item, signature=b"wrong")
        with self.assertRaisesRegex(ArtifactRegistryError, "signature") as raised:
            registry.resolve(item)
        self.assertEqual("ARTIFACT_SIGNATURE_INVALID", raised.exception.code)

    def test_wrong_digest_fails_closed(self) -> None:
        item = reference(artifact_digest="sha256:" + "a" * 64)
        registry = self.registry(item)
        with self.assertRaisesRegex(ArtifactRegistryError, "artifact digest") as raised:
            registry.resolve(item)
        self.assertEqual("ARTIFACT_DIGEST_MISMATCH", raised.exception.code)

    def test_wrong_tenant_or_catalog_version_cannot_resolve(self) -> None:
        item = reference()
        registry = self.registry(item)
        for wrong in (
            ArtifactReference("tenant-other", item.catalog_version, item.artifact_digest, item.code_digest),
            ArtifactReference(item.tenant_id, "sha256:" + "e" * 64, item.artifact_digest, item.code_digest),
        ):
            with self.assertRaisesRegex(ArtifactRegistryError, "not registered") as raised:
                registry.resolve(wrong)
            self.assertEqual("ARTIFACT_NOT_FOUND", raised.exception.code)

    def test_revoked_artifact_fails_closed_even_when_cached(self) -> None:
        item = reference()
        registry = self.registry(item, revoked=frozenset({item.artifact_digest}))
        with self.assertRaisesRegex(ArtifactRegistryError, "revoked") as raised:
            registry.resolve(item)
        self.assertEqual("ARTIFACT_REVOKED", raised.exception.code)

    def test_cache_verifies_once_and_is_scoped_to_the_full_reference(self) -> None:
        item = reference()
        registry = self.registry(item)
        self.assertEqual(registry.resolve(item), registry.resolve(item))
        self.assertEqual(1, registry.verification_count)
        wrong_tenant = ArtifactReference("tenant-other", item.catalog_version, item.artifact_digest, item.code_digest)
        with self.assertRaises(ArtifactRegistryError):
            registry.resolve(wrong_tenant)
        self.assertEqual(1, registry.verification_count)

    def test_assignment_time_envelopes_are_bounded_and_cannot_replace_identity(self) -> None:
        first = reference(tenant_id="tenant-one")
        second = reference(tenant_id="tenant-two")
        registry = ImmutableArtifactRegistry((), {"registry-key": public_key(SEED)}, cache_max_entries=1)
        registry.resolve_signed(artifact(first))
        with self.assertRaisesRegex(ArtifactRegistryError, "different signed material") as raised:
            registry.resolve_signed(artifact(first, bytes_value=b"replacement"))
        self.assertEqual("ARTIFACT_IDENTITY_CONFLICT", raised.exception.code)
        registry.resolve_signed(artifact(second))
        with self.assertRaisesRegex(ArtifactRegistryError, "not registered"):
            registry.resolve(first)
        self.assertEqual(registry.resolve(second), artifact(second).bytes)
