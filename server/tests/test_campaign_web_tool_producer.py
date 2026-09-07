import hashlib
import json
from pathlib import Path

import pytest

import campaign_web_tool_producer as producer
import campaign_web_tool_static as static


# --- Fakes for the trusted-interaction path, used only to reach the byte-
# comparison branch deterministically. The real-browser success test below
# never uses these: it launches actual Chromium.

class _FakeDownload:
    def __init__(self, content):
        self._content = content

    def save_as(self, path):
        Path(path).write_bytes(self._content)


class _FakeExpectDownload:
    def __init__(self, content):
        self._content = content
        self.value = None

    def __enter__(self):
        self.value = _FakeDownload(self._content)
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def body(self):
        return self._body


class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def input_value(self, timeout=None):
        return self._page.records_text

    def click(self, timeout=None):
        return None

    def inner_text(self, timeout=None):
        return self._page.preview_text


class _FakePage:
    def __init__(self, html_bytes, records_text, preview_text, downloaded_bytes):
        self.html_bytes = html_bytes
        self.records_text = records_text
        self.preview_text = preview_text
        self.downloaded_bytes = downloaded_bytes

    def on(self, event, handler):
        return None

    def goto(self, url, timeout=None):
        return _FakeResponse(self.html_bytes)

    def evaluate(self, expression):
        return 'null'

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def expect_download(self, timeout=None):
        return _FakeExpectDownload(self.downloaded_bytes)


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def route(self, pattern, handler):
        return None

    def new_page(self):
        return self._page

    def close(self):
        return None


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.version = 'fake-0'

    def new_context(self, **kwargs):
        return self._context

    def close(self):
        return None


class _FakeContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_start_browser(html_bytes, records_text, preview_text, downloaded_bytes):
    page = _FakePage(html_bytes, records_text, preview_text, downloaded_bytes)
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    return lambda: (_FakeContextManager(), browser)


# --- Injected template is refused before any browser is launched --------------

def test_injected_template_is_rejected_before_any_browser_launch(monkeypatch):
    source = json.dumps([{'a': 1}]).encode()
    html = static.render(source)
    tampered = html.replace(b'Convert', b'Cxnvert', 1)
    launched = []
    monkeypatch.setattr(producer, '_start_browser', lambda: launched.append(True))
    with pytest.raises(producer.WebToolVerificationError):
        producer.verify(tampered, source)
    assert launched == []


# --- Missing Playwright/browser is typed unavailable ---------------------------

def test_missing_browser_is_typed_unavailable(monkeypatch):
    source = json.dumps([{'a': 1}]).encode()
    html = static.render(source)

    def _boom():
        raise producer.WebToolUnavailable('playwright is not installed: simulated')

    monkeypatch.setattr(producer, '_start_browser', _boom)
    with pytest.raises(producer.WebToolUnavailable):
        producer.verify(html, source)


# --- Bounded failed downloaded-byte comparison, trusted local interception ----

def test_wrong_downloaded_bytes_are_a_typed_verification_failure(monkeypatch):
    records = [{'a': 1}, {'a': 2}]
    source = json.dumps(records).encode()
    html = static.render(source)
    headers = static._header_union(records)
    preview_text = f'Rows: {len(records)} | Columns: {len(headers)}'
    monkeypatch.setattr(producer, '_start_browser',
                         _fake_start_browser(html, json.dumps(records), preview_text, b'not,the,right,csv\r\n'))
    with pytest.raises(producer.WebToolVerificationError):
        producer.verify(html, source)


def test_wrong_preview_text_is_a_typed_verification_failure(monkeypatch):
    records = [{'a': 1}]
    source = json.dumps(records).encode()
    html = static.render(source)
    expected_csv = static.expected_output(source)
    monkeypatch.setattr(producer, '_start_browser',
                         _fake_start_browser(html, json.dumps(records), 'Rows: 99 | Columns: 1', expected_csv))
    with pytest.raises(producer.WebToolVerificationError):
        producer.verify(html, source)


# --- Real Chromium success: cannot be mocked -----------------------------------

def test_real_chromium_produces_the_exact_downloaded_csv():
    records = [{'name': 'Alice', 'age': 30, 'active': True, 'note': None},
               {'name': 'Bob', 'age': 25, 'active': False, 'note': 'ok'}]
    source = json.dumps(records).encode()
    html = static.render(source)
    result = producer.verify(html, source, timeout_s=30)
    expected = static.expected_output(source)
    assert result['output']['content'].encode('utf-8') == expected
    assert result['output']['content_valid'] is True
    assert result['output']['bytes_verified'] is True
    assert result['output']['sha256'] == hashlib.sha256(expected).hexdigest()
    assert result['output']['name'] == 'records.csv'
    assert result['recipe_id'] == static.RECIPE_ID
    assert result['recipe_version'] == static.RECIPE_VERSION
    assert result['source_revision'] == hashlib.sha256(html).hexdigest()
    assert 'clicked Convert' in result['workflow']
    assert any('opaque' in observation for observation in result['observations'])


def test_busy_browser_capacity_preserves_work_without_launching(monkeypatch):
    source = b'[{"name":"Example"}]'
    launched = []
    monkeypatch.setattr(producer, '_start_browser', lambda: launched.append(True))
    assert producer._VERIFY_SLOT.acquire(blocking=False)
    try:
        with pytest.raises(producer.WebToolUnavailable, match='capacity'):
            producer.verify(static.render(source), source)
    finally:
        producer._VERIFY_SLOT.release()
    assert launched == []
