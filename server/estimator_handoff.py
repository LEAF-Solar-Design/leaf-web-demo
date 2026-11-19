"""Studio-only Estimator authority and KMS handoff. Never retain channel/token values."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from types import MappingProxyType

import deps
from demo_actor import resolve_demo_actor
from investor_demo import SUBJECT_PREFIX

logger = logging.getLogger(__name__)
ESTIMATOR_ORIGINS = MappingProxyType({
    "https://estimator-staging.leafdesign.ai": ("studio-staging", "staging"),
    "https://estimator-studio-staging-973968415611.us-central1.run.app": ("studio-staging", "staging"),
})
ISSUERS = MappingProxyType({"staging": "https://platform-staging.leafdesign.ai", "local": "http://localhost:3000"})
CHANNEL = re.compile(r"[A-Za-z0-9_-]{43}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,512}\.[A-Za-z0-9_-]{1,1024}\.[A-Za-z0-9_-]{300,700}")


def canonical_uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate member")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("non-finite number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def origin_entry(origin, posture):
    if type(origin) is not str:
        return None
    entry = ESTIMATOR_ORIGINS.get(origin)
    if entry and posture in ISSUERS and (posture != "staging" or entry[1] == "staging"):
        return entry
    if posture == "local":
        match = re.fullmatch(r"http://localhost:([1-9][0-9]{0,4})", origin)
        if match and int(match[1]) <= 65535:
            return ("dev-local", "dev")
    return None


@dataclass(frozen=True)
class Binding:
    tenant_id: str
    studio_project_id: str
    deployment_id: str
    estimator_origin: str
    project: str


def parse_bindings(raw, posture):
    if type(raw) is not str or len(raw.encode("utf-8")) > 32768 or posture not in ISSUERS:
        raise ValueError("bindings refused")
    rows = strict_json(raw)
    if type(rows) is not list or len(rows) > 32:
        raise ValueError("bindings refused")
    deployments, origins, projects = set(), set(), set()
    result = []
    for row in rows:
        if type(row) is not dict or set(row) != set(Binding.__dataclass_fields__):
            raise ValueError("bindings refused")
        if any(type(value) is not str for value in row.values()):
            raise ValueError("bindings refused")
        entry = origin_entry(row["estimator_origin"], posture)
        pair = (row["tenant_id"], row["studio_project_id"])
        if (not all(canonical_uuid(value) for value in pair)
                or not re.fullmatch(r"[a-z0-9-]{1,32}", row["project"])
                or entry is None or row["deployment_id"] != entry[0]
                or row["deployment_id"] in deployments or row["estimator_origin"] in origins
                or pair in projects):
            raise ValueError("bindings refused")
        deployments.add(row["deployment_id"])
        origins.add(row["estimator_origin"])
        projects.add(pair)
        result.append(Binding(**row))
    return tuple(result)


@lru_cache(maxsize=1)
def configured_bindings(raw, posture):
    try:
        return parse_bindings(raw, posture)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        logger.warning("Estimator bindings refused")
        return ()


def human_caller(tenant):
    subject = getattr(tenant, "subject", None)
    return (deps.auth_live() and isinstance(tenant, deps.TenantContext)
            and tenant.authority_resolved is True and tenant.backedge is False
            and tenant.verified_demo is False and type(subject) is str
            and 0 < len(subject) <= 512 and subject.strip() == subject
            and not subject.endswith("@clients") and not subject.startswith(SUBJECT_PREFIX)
            and resolve_demo_actor(tenant) is None)


class MintLimiter:
    def __init__(self):
        self.lock = Lock()
        self.subjects = OrderedDict()

    def admit(self, subject, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            # Insertion order gives a bounded, oldest-first subject eviction.
            times = self.subjects.get(subject, deque())
            while times and times[0] <= now - 300:
                times.popleft()
            self.subjects[subject] = times
            while len(self.subjects) > 10000:
                self.subjects.popitem(last=False)
            if len(times) >= 20:
                return max(1, math.ceil(times[0] + 300 - now))
            times.append(now)
            return 0


limiter = MintLimiter()


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encoded(value):
    return b64(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def claims_for(binding, subject, chan, posture, now=None):
    if (not isinstance(binding, Binding) or not canonical_uuid(binding.tenant_id)
            or not canonical_uuid(binding.studio_project_id) or type(subject) is not str
            or not 0 < len(subject) <= 512 or type(chan) is not str or not CHANNEL.fullmatch(chan)
            or posture not in ISSUERS or origin_entry(binding.estimator_origin, posture) is None
            or origin_entry(binding.estimator_origin, posture)[0] != binding.deployment_id
            or not re.fullmatch(r"[a-z0-9-]{1,32}", binding.project)):
        raise ValueError("handoff refused")
    iat = int(time.time()) if now is None else now
    if type(iat) is not int or iat < 0:
        raise ValueError("handoff refused")
    aud = "leaf-estimator:" + binding.deployment_id
    sub = "sh_" + hashlib.sha256(("leaf-estimator-sub|v2|" + aud + "|" + subject).encode()).hexdigest()[:32]
    return {"ver": 2, "iss": ISSUERS[posture], "aud": aud, "sub": sub,
            "tenant": binding.tenant_id, "sproj": binding.studio_project_id, "project": binding.project,
            "iat": iat, "nbf": iat, "exp": iat + 60, "jti": secrets.token_urlsafe(16), "chn": chan}


class KmsRs256Signer:
    def __init__(self, key_id, client=None):
        self.key_id = key_id
        self.client = client
        self.lock = Lock()
        self.kid = None
        self.signature_bytes = None

    def sign(self, claims):
        with self.lock:
            if self.client is None:
                import boto3
                from botocore.config import Config
                self.client = boto3.client("kms", region_name=os.environ.get("AWS_REGION") or "us-east-1",
                                           config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 1}))
            if self.kid is None:
                from cryptography.hazmat.primitives.serialization import load_der_public_key
                from cryptography.hazmat.primitives.asymmetric import rsa
                response = self.client.get_public_key(KeyId=self.key_id)
                key = load_der_public_key(response["PublicKey"])
                kid = response["KeyId"]
                if (not isinstance(key, rsa.RSAPublicKey) or key.key_size not in (2048, 3072)
                        or key.public_numbers().e != 65537 or type(kid) is not str
                        or not re.fullmatch(r"arn:aws:kms:[a-z0-9-]{1,32}:[0-9]{12}:key/[a-f0-9-]{36}", kid)):
                    raise ValueError("signer refused")
                self.signature_bytes = key.key_size // 8
                self.kid = kid
            header = {"alg": "RS256", "typ": "leaf-estimator-handoff+jwt", "kid": self.kid}
            message = (encoded(header) + "." + encoded(claims)).encode("ascii")
            signature = self.client.sign(KeyId=self.kid, Message=message, MessageType="RAW",
                                         SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")["Signature"]
            if len(signature) != self.signature_bytes:
                raise ValueError("signer refused")
            token = message.decode("ascii") + "." + b64(signature)
            if len(token) > 2048 or not TOKEN.fullmatch(token):
                raise ValueError("handoff refused")
            return token


_signer = None
_signer_lock = Lock()


def signer():
    global _signer
    with _signer_lock:
        if _signer is None:
            key_id = os.environ.get("LEAF_ESTIMATOR_HANDOFF_KMS_KEY_ID", "")
            if not key_id or len(key_id) > 256:
                raise ValueError("signer unavailable")
            _signer = KmsRs256Signer(key_id)
        return _signer
