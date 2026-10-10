"""The one text comparison every request-fed pin, digest and secret check goes through."""
from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import text_match  # noqa: E402
from text_match import text_matches  # noqa: E402


ACCENTED = "\u00e9"
DECOMPOSED = "e\u0301"
FULLWIDTH_ZERO = "\uff10"
LONE_HIGH = "\ud800"
LONE_LOW = "\udce9"
TEXTS = (
    "", "a", "broker-secret", "sha256:" + "0" * 64, ACCENTED, DECOMPOSED,
    FULLWIDTH_ZERO * 64, "sha256:" + ACCENTED * 64, "\U0001f600", "tab\tand\nline", "nul\x00byte",
)
UNENCODABLE = (LONE_HIGH, LONE_LOW, "ok" + LONE_HIGH, LONE_LOW + "ok")
NOT_TEXT = (None, 0, 1, True, False, 1.5, b"", b"broker-secret", bytearray(b"broker-secret"),
            memoryview(b"broker-secret"), ["broker-secret"], ("broker-secret",), {"a": 1}, object())


class Shouting(str):
    """A str subclass that lies about itself everywhere a subclass can."""

    def encode(self, *args, **kwargs):  # noqa: D401
        return b"broker-secret"

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0


def test_text_match_equal_text_matches():
    for text in TEXTS:
        assert text_matches(text, text) is True, text
        # a second, separately built copy: equality is by content, never by identity
        assert text_matches("".join(list(text)), text) is True, text


def test_text_match_unequal_text_does_not_match():
    for left, right in itertools.permutations(TEXTS, 2):
        assert text_matches(left, right) is False, (left, right)
    secret = "broker-secret"
    for near in (secret + " ", " " + secret, secret + "\n", secret + "\x00", secret[:-1],
                 secret + "t", secret.upper(), secret.title(), secret.replace("-", "\u2010"),
                 secret + ACCENTED, ACCENTED + secret):
        assert text_matches(near, secret) is False, near
        assert text_matches(secret, near) is False, near


def test_text_match_never_normalises():
    # the composed and the decomposed spelling look alike and are different text
    assert ACCENTED != DECOMPOSED
    assert text_matches(ACCENTED, DECOMPOSED) is False
    assert text_matches(DECOMPOSED, ACCENTED) is False
    assert text_matches("0" * 64, FULLWIDTH_ZERO * 64) is False
    assert text_matches("A", "a") is False


def test_text_match_refuses_anything_that_is_not_text():
    for value in NOT_TEXT:
        assert text_matches(value, "broker-secret") is False, value
        assert text_matches("broker-secret", value) is False, value
        assert text_matches(value, value) is False, value
    # bytes that spell the same secret are still not text
    assert text_matches(b"broker-secret", "broker-secret") is False
    assert text_matches("broker-secret", b"broker-secret") is False


def test_text_match_unencodable_text_never_matches_and_never_raises():
    for value in UNENCODABLE:
        assert text_matches(value, value) is False, value
        assert text_matches(value, "broker-secret") is False, value
        assert text_matches("broker-secret", value) is False, value
        for other in UNENCODABLE:
            assert text_matches(value, other) is False, (value, other)


def test_text_match_reads_a_subclass_by_its_characters():
    liar = Shouting("wrong")
    assert liar == "broker-secret"  # the subclass claims equality with everything
    assert text_matches(liar, "broker-secret") is False
    assert text_matches("broker-secret", liar) is False
    assert text_matches(liar, "wrong") is True
    assert text_matches("wrong", Shouting("wrong")) is True
    assert text_matches(Shouting(LONE_HIGH), LONE_HIGH) is False


def test_text_match_total_over_every_pair():
    values = (*TEXTS, *UNENCODABLE, *NOT_TEXT)
    for left, right in itertools.product(values, repeat=2):
        expected = (
            type(left) is str and type(right) is str
            and left not in UNENCODABLE and right not in UNENCODABLE
            and left == right
        )
        outcome = text_matches(left, right)
        assert outcome is expected, (left, right)


def test_text_match_uses_the_constant_time_primitive_on_bytes(monkeypatch):
    calls = []

    def spy(left, right):
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(text_match, "hmac", SimpleNamespace(compare_digest=spy))
    assert text_matches("sha256:" + ACCENTED, "sha256:" + ACCENTED) is True
    assert text_matches("stale", "current") is False
    assert calls == [
        (("sha256:" + ACCENTED).encode("utf-8"), ("sha256:" + ACCENTED).encode("utf-8")),
        (b"stale", b"current"),
    ]
    assert all(type(side) is bytes for call in calls for side in call)
    del calls[:]
    for value in (*NOT_TEXT, *UNENCODABLE):
        assert text_matches(value, "current") is False
        assert text_matches("current", value) is False
    assert calls == []


def test_text_match_long_text():
    long = "x" * (1 << 20)
    assert text_matches(long, "x" * (1 << 20)) is True
    assert text_matches(long, long[:-1] + "y") is False
    assert text_matches(long, long[:-1]) is False
    assert text_matches(long + ACCENTED, long + ACCENTED) is True
    assert text_matches(long + ACCENTED, long + DECOMPOSED) is False


def test_text_match_module_is_a_leaf():
    # Every server module may import this one, so it may import nothing of ours.
    tree = ast.parse(Path(text_match.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"__future__", "hmac"}
