"""Real-Chromium producer half of the trusted-template JSON-to-CSV web tool.

`verify` executes the immutable server-rendered document
(`campaign_web_tool_static.render`) in a fresh, network-denied browser
context and proves the download it produces is byte-identical to the
independent Python `expected_output`. It never runs arbitrary tenant HTML or
JavaScript: `validate_generated` refuses anything but the exact trusted
template before a browser is ever launched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from threading import BoundedSemaphore
from pathlib import Path

import campaign_delivery_service as delivery
import campaign_web_tool_static as static

_SYNTHETIC_URL = 'https://completion.invalid/release'
_VERIFY_SLOT = BoundedSemaphore(1)

# Small, explicit allowlist: only what a Chromium child process needs to start
# on Windows or Linux. No inherited secret ever reaches the browser process
# this way, regardless of what the caller's own environment holds.
_ENV_ALLOWLIST = {
    'PATH', 'SYSTEMROOT', 'SYSTEMDRIVE', 'PROGRAMDATA', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC', 'PATHEXT',
    'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
    'HOMEDRIVE', 'HOMEPATH', 'PLAYWRIGHT_BROWSERS_PATH', 'HOME', 'LANG',
}


class WebToolUnavailable(RuntimeError):
    """Playwright, or its browser binary, is not installed/reachable."""


class WebToolVerificationError(RuntimeError):
    """The trusted document or its downloaded output failed verification."""


def _bounded_message(error):
    text = str(error).strip()
    text = text.splitlines()[0] if text else error.__class__.__name__
    return text[:300]


def _bounded_env():
    return {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOWLIST}


def _browser_mode():
    mode = os.environ.get('LEAF_MANAGED_WEB_BROWSER_MODE', 'sandboxed')
    if mode not in ('sandboxed', 'trusted-template-container'):
        raise WebToolUnavailable('Invalid LEAF_MANAGED_WEB_BROWSER_MODE configuration')
    return mode


def _start_browser():
    """Import + launch, as one unit: any failure here is UNAVAILABLE, never a
    verification failure. Cleans itself up completely on a launch failure.
    """
    mode = _browser_mode()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise WebToolUnavailable(_bounded_message(f'playwright is not installed: {error}')) from error
    context_manager = sync_playwright()
    context_manager._leaf_managed_web_browser_mode = mode
    try:
        playwright = context_manager.__enter__()
    except Exception as error:
        raise WebToolUnavailable(_bounded_message(error)) from error
    try:
        # Container mode is only for this byte-exact trusted template, never
        # arbitrary tenant HTML. A failed sandboxed launch never changes mode.
        browser = playwright.chromium.launch(
            headless=True, chromium_sandbox=mode == 'sandboxed',
            env=_bounded_env(), timeout=15000)
    except Exception as error:
        context_manager.__exit__(None, None, None)
        raise WebToolUnavailable(_bounded_message(error)) from error
    return context_manager, browser


def verify(html_bytes, source_bytes, *, timeout_s=30):
    if not _VERIFY_SLOT.acquire(blocking=False):
        raise WebToolUnavailable('Browser verification capacity is in use; retry the verification stage')
    try:
        return _verify(html_bytes, source_bytes, timeout_s=timeout_s)
    finally:
        _VERIFY_SLOT.release()


def _verify(html_bytes, source_bytes, *, timeout_s=30):
    try:
        static.validate_generated(html_bytes, source_bytes)
    except delivery.DeliveryConflict as error:
        raise WebToolVerificationError(_bounded_message(error)) from error

    expected_csv = static.expected_output(source_bytes)
    expected_meta = delivery.validate_bytes('records.csv', expected_csv)
    records = static._parse_records(source_bytes)
    headers = static._header_union(records)
    source_records = json.loads(source_bytes.decode('utf-8'))

    context_manager, browser = _start_browser()
    mode = context_manager._leaf_managed_web_browser_mode
    workflow = []
    observations = [
        f'chromium {browser.version} launched headless in {mode} mode with '
        f'chromium_sandbox={mode == "sandboxed"}; '
        + ('OS browser sandboxing was requested as defense in depth, not proven kernel isolation'
           if mode == 'sandboxed' else
           'OS browser sandboxing was disabled for the server-owned exact trusted template; '
           'template equality is the executable-code admission boundary'),
        'the context routed every request itself: only the one synthetic release URL was fulfilled and '
        'every other destination was aborted, which is application-level test isolation, not a kernel sandbox',
    ]
    tmp_dir = tempfile.mkdtemp(prefix='leaf-web-tool-download-')
    context = None
    downloaded_bytes = None
    source_revision = None
    try:
        try:
            context = browser.new_context(accept_downloads=True, service_workers='block')

            def _route(route):
                if route.request.url == _SYNTHETIC_URL:
                    route.fulfill(status=200, body=html_bytes, headers={
                        'content-type': 'text/html; charset=utf-8',
                        'content-security-policy': static.CSP,
                        'x-content-type-options': 'nosniff',
                    })
                else:
                    route.abort()

            context.route('**/*', _route)
            page = context.new_page()
            page.on('popup', lambda popup: popup.close())

            response = page.goto(_SYNTHETIC_URL, timeout=timeout_s * 1000)
            workflow.append('loaded the trusted document at the synthetic release URL')
            body = response.body()
            if body != html_bytes:
                raise WebToolVerificationError('Served document body diverged from the trusted template')
            source_revision = hashlib.sha256(body).hexdigest()

            origin = page.evaluate('window.origin')
            if origin != 'null':
                raise WebToolVerificationError(f'Document origin was not opaque: {origin!r}')
            observations.append("window.origin read back the opaque string 'null'")

            initial_value = page.locator('#records-input').input_value()
            if json.loads(initial_value) != source_records:
                raise WebToolVerificationError('Initial textarea content diverged from the source records')
            workflow.append('read the initial JSON textarea value')

            page.locator('#convert-btn').click(timeout=timeout_s * 1000)
            workflow.append('clicked Convert')
            expected_preview = f'Rows: {len(records)} | Columns: {len(headers)}'
            preview_text = page.locator('#preview').inner_text(timeout=timeout_s * 1000)
            if preview_text != expected_preview:
                raise WebToolVerificationError(
                    f'Preview text {preview_text!r} did not match {expected_preview!r}')
            workflow.append('read the row/column preview')

            with page.expect_download(timeout=timeout_s * 1000) as download_info:
                page.locator('#download-btn').click(timeout=timeout_s * 1000)
            download = download_info.value
            workflow.append('clicked Download CSV and observed a real download event')
            saved_path = Path(tmp_dir) / 'records.csv'
            download.save_as(str(saved_path))
            downloaded_bytes = saved_path.read_bytes()
            workflow.append('saved the downloaded file and read its bytes back')
        except (WebToolVerificationError, WebToolUnavailable):
            raise
        except Exception as error:
            raise WebToolVerificationError(_bounded_message(error)) from error
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        try:
            browser.close()
        finally:
            context_manager.__exit__(None, None, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if downloaded_bytes != expected_csv:
        raise WebToolVerificationError(
            'Downloaded CSV bytes did not equal the independent Python expected_output')
    observed_meta = delivery.validate_bytes('records.csv', downloaded_bytes)
    if (observed_meta['sha256'] != expected_meta['sha256']
            or observed_meta['size_bytes'] != expected_meta['size_bytes']):
        raise WebToolVerificationError('Downloaded CSV metadata diverged from the independent expected output')

    return {
        'source_revision': source_revision,
        'recipe_id': static.RECIPE_ID,
        'recipe_version': static.RECIPE_VERSION,
        'workflow': workflow,
        'observations': observations,
        'output': {
            'name': 'records.csv',
            'content': downloaded_bytes.decode('utf-8'),
            'sha256': observed_meta['sha256'],
            'size_bytes': observed_meta['size_bytes'],
            'media_type': observed_meta['media_type'],
            'format': observed_meta['format'],
            'content_valid': True,
            'bytes_verified': True,
        },
    }
