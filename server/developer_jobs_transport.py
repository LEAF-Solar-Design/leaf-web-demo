"""Authenticated transport for the campaign broker's developer service.

Endpoint and role are trusted server configuration. Credentials stay in process;
responses and exceptions never expose signed headers or remote error bodies.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.config import Config


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
CHUNK_BYTES = 65536
ACTIONS = frozenset({"submit", "inspect", "cancel", "retry", "recover"})


class TransportError(Exception):
    def __init__(self, code: str, status: int = 502):
        self.code = code
        self.status = status
        super().__init__(code)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


class DeveloperJobsTransport:
    """One signed POST per call, with no mutation retry or redirect following."""

    def __init__(self, endpoint: str, region: str = "us-east-1",
                 role_arn: str | None = None, *, boto3_session=None,
                 http_session=None):
        if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
            raise TransportError("invalid_endpoint", 500)
        if not isinstance(endpoint, str):
            raise TransportError("invalid_endpoint", 500)
        # Compare the whole URL as well as its parsed components. This excludes
        # ports, escaped host/path spellings, whitespace and empty ?/# suffixes.
        pattern = rf"https://[a-z0-9]{{10}}\.execute-api\.{re.escape(region)}\.amazonaws\.com/v1/developer/jobs"
        if not re.fullmatch(pattern, endpoint):
            raise TransportError("invalid_endpoint", 500)
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TransportError("invalid_endpoint", 500)
        if role_arn is not None and (not isinstance(role_arn, str) or not re.fullmatch(
                r"arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}", role_arn)):
            raise TransportError("invalid_role", 500)
        self.endpoint = endpoint
        self.region = region
        self.role_arn = role_arn
        self._session = boto3_session if boto3_session is not None else boto3.Session()
        self._http = http_session if http_session is not None else requests.Session()
        if http_session is None:
            self._http.trust_env = False

    def _credentials(self):
        try:
            if self.role_arn is not None:
                values = self._session.client("sts", region_name=self.region,
                    config=Config(connect_timeout=5, read_timeout=10,
                                  retries={"total_max_attempts": 1})).assume_role(
                    RoleArn=self.role_arn, RoleSessionName="campaign-developer-broker")["Credentials"]
                credentials = Credentials(values["AccessKeyId"], values["SecretAccessKey"], values["SessionToken"])
            else:
                credentials = self._session.get_credentials()
            if credentials is None:
                raise ValueError("credentials unavailable")
            return credentials.get_frozen_credentials()
        except Exception:
            raise TransportError("credentials_unavailable", 503) from None

    def request(self, body: dict) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("action"), str) or body["action"] not in ACTIONS:
            raise TransportError("invalid_action", 400)
        try:
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise TransportError("invalid_request", 400) from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise TransportError("request_too_large", 413)
        credentials = self._credentials()
        try:
            request = AWSRequest(method="POST", url=self.endpoint, data=encoded,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
            SigV4Auth(credentials, "execute-api", self.region).add_auth(request)
            headers = dict(request.headers.items())
        except Exception:
            raise TransportError("signing_failed", 503) from None
        response = None
        try:
            response = self._http.post(self.endpoint, data=encoded, headers=headers,
                                       timeout=(5, 30), allow_redirects=False, stream=True)
            status = response.status_code
            if 300 <= status < 400:
                raise TransportError("redirect_refused", status)
            if not 200 <= status < 300:
                raise TransportError("developer_http_error", status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise TransportError("non_json_response")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]{1,10}", str(length)):
                    raise TransportError("invalid_response_length")
                if int(length) > MAX_RESPONSE_BYTES:
                    raise TransportError("response_too_large")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if not isinstance(chunk, bytes):
                    raise TransportError("invalid_response")
                if len(chunk) > CHUNK_BYTES or len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise TransportError("response_too_large")
                content.extend(chunk)
            try:
                result = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                                    parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise TransportError("invalid_json_response") from None
            if not isinstance(result, dict):
                raise TransportError("invalid_response_shape")
            return result
        except (requests.exceptions.Timeout, TimeoutError):
            raise TimeoutError("developer transport timed out") from None
        except (requests.exceptions.RequestException, ConnectionError, OSError):
            raise ConnectionError("developer transport connection failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    __call__ = request
