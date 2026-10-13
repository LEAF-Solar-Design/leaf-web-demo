"""Host-only, per-run project proof identity. Never package this policy in images."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Mapping, NamedTuple, Any

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

ISSUER_PREFIX = "https://proof-project.leaf.invalid/"
SUBJECT_PREFIX = "leaf-proof-project|"
MAX_LIFETIME_S = 3600
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
REFUSAL_CODES = frozenset({"profile", "posture", "run_id", "jwks", "key", "claims"})
_PRIVATE_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})


class FixtureRefused(Exception):
    def __init__(self, code: str):
        if code not in REFUSAL_CODES:
            raise ValueError("unknown fixture refusal code")
        self.code = code
        super().__init__(code)


class FixtureVerification(NamedTuple):
    public_key: Any
    issuer: str
    audience: str
    kid: str


def _thumbprint(jwk: dict) -> str:
    canonical = json.dumps({k: jwk[k] for k in ("e", "kty", "n")},
                           sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode("ascii")


def _run_identity(run_id: str) -> tuple[str, str]:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise FixtureRefused("run_id")
    return ISSUER_PREFIX + run_id + "/", "leaf-proof-project:" + run_id


def fixture_verification(header: dict, unverified_claims: dict,
                         environ: Mapping[str, str]) -> FixtureVerification:
    """Resolve only a pinned, public, single-key fixture JWKS; never use a URL."""
    if environ.get("LEAF_PROOF_PROFILE") != "project":
        raise FixtureRefused("profile")
    if environ.get("LEAF_RUNTIME_ENV", "").strip().lower() not in {"local", "test"}:
        raise FixtureRefused("posture")
    issuer, audience = _run_identity(environ.get("LEAF_PROOF_PROJECT_RUN_ID", ""))
    try:
        data = json.loads(Path(environ["LEAF_PROOF_PROJECT_JWKS_FILE"]).read_text(encoding="utf-8"))
        keys = data["keys"]
        if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], dict):
            raise ValueError
        jwk = keys[0]
        if (_PRIVATE_MEMBERS.intersection(jwk) or jwk.get("kty") != "RSA" or
                jwk.get("alg") != "RS256" or jwk.get("use") != "sig"):
            raise ValueError
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        # Round-trip the public numbers to refuse noncanonical base64url encodings.
        canonical = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
        if public_key.key_size != 2048 or any(jwk[k] != canonical[k] for k in ("n", "e")):
            raise ValueError
        kid = _thumbprint(jwk)
        if jwk.get("kid") != kid or environ.get("LEAF_PROOF_PROJECT_KEY_SHA256") != kid:
            raise FixtureRefused("key")
    except FixtureRefused:
        raise
    except Exception:
        raise FixtureRefused("jwks") from None
    if (header.get("alg") != "RS256" or header.get("kid") != kid or
            unverified_claims.get("iss") != issuer or unverified_claims.get("aud") != audience):
        raise FixtureRefused("claims")
    return FixtureVerification(public_key, issuer, audience, kid)


class FixtureIssuer:
    """One in-memory private key; only the public JWKS can be written to disk."""
    def __init__(self, run_id: str):
        self.issuer, self.audience = _run_identity(run_id)
        self.run_id = run_id
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key()))
        self.kid = self.thumbprint = _thumbprint(self._jwk)
        self._jwk.update(alg="RS256", use="sig", kid=self.kid)

    @classmethod
    def create(cls, run_id: str) -> FixtureIssuer:
        return cls(run_id)

    def public_jwks(self) -> dict:
        return {"keys": [dict(self._jwk)]}

    def write_public_jwks(self, path):
        """Publish a complete file atomically with a no-clobber hard link."""
        path = Path(path)
        fd, temporary = tempfile.mkstemp(prefix=".proof-project-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.public_jwks(), stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)  # Atomic on Windows and POSIX; EEXIST refuses overwrite.
        finally:
            os.unlink(temporary)

    def profile_env(self, jwks_path) -> dict[str, str]:
        return {"LEAF_PROOF_PROFILE": "project",
                "LEAF_PROOF_PROJECT_RUN_ID": self.run_id,
                "LEAF_PROOF_PROJECT_KEY_SHA256": self.thumbprint,
                "LEAF_PROOF_PROJECT_JWKS_FILE": str(jwks_path)}

    def mint(self, subject_suffix, *, tenant_id, org_id, tier, claim_ns,
             lifetime_s=900, now=None) -> str:
        if type(lifetime_s) is not int or not 60 <= lifetime_s <= MAX_LIFETIME_S:
            raise ValueError("fixture lifetime must be 60..3600 seconds")
        issued = int(time.time() if now is None else now)
        ns = claim_ns if claim_ns.endswith("/") else claim_ns + "/"
        claims = {"iss": self.issuer, "aud": self.audience,
                  "sub": SUBJECT_PREFIX + subject_suffix, "iat": issued,
                  "nbf": issued, "exp": issued + lifetime_s,
                  ns + "tenant_id": tenant_id, ns + "org_id": org_id, ns + "tier": tier}
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers={"kid": self.kid})
