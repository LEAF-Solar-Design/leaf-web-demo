"""Guard static misses and app-shell caching in the web nginx config."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


CONFIG = Path(__file__).resolve().parent.parent / "deploy" / "nginx.conf"
STATIC_EXTENSIONS = (
    "woff", "woff2", "ttf", "otf", "eot", "svg", "png", "jpg", "jpeg",
    "gif", "webp", "avif", "ico", "css", "js", "mjs", "map",
    "webmanifest", "txt", "xml",
)


def _config_text():
    return re.sub(r"#.*", "", CONFIG.read_text(encoding="utf-8"))


def _location(selector):
    match = re.search(
        r"\blocation\s+" + selector + r"\s*\{([^{}]*)\}", _config_text()
    )
    assert match is not None, f"missing nginx location: {selector}"
    return match


def test_static_location_returns_404_before_spa_fallback():
    static = _location(r"~\*\s+(\S+)")
    pattern, body = static.groups()
    assert static.start() < _location(r"/").start()
    assert re.search(r"\btry_files\s+\$uri\s+=404\s*;", body)
    assert not re.search(r"\b(?:add_header|expires|etag|if_modified_since)\b", body)
    matcher = re.compile(pattern, re.IGNORECASE)
    for extension in STATIC_EXTENSIONS:
        path = f"/nested/file.{extension}"
        assert matcher.search(path), path
        assert matcher.search(path.upper()), path.upper()
        assert not matcher.search(path + "/route"), path
    assert not matcher.search("/app/route")


def test_index_html_revalidates_and_spa_fallback_is_preserved():
    index = _location(r"=\s+/index\.html").group(1)
    assert re.search(r'\badd_header\s+Cache-Control\s+"no-cache"\s*;', index)
    fallback = _location(r"/").group(1)
    assert re.search(r"\btry_files\s+\$uri\s+\$uri/\s+/index\.html\s*;", fallback)


def test_assets_keep_immutable_caching_and_bypass_static_regex():
    assets = _location(r"\^~\s+/assets/").group(1)
    assert re.search(r"\btry_files\s+\$uri\s+=404\s*;", assets)
    assert re.search(r"\bexpires\s+1y\s*;", assets)
    assert re.search(r'\badd_header\s+Cache-Control\s+"public, immutable"\s*;', assets)


def test_nginx_config_syntax_when_available(tmp_path):
    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("nginx binary is not on PATH; static config checks still apply")

    # The deployed file is a server snippet included by nginx's http block.
    wrapper = tmp_path / "nginx.conf"
    wrapper.write_text(
        "error_log stderr;\n"
        f'pid "{tmp_path / "nginx.pid"}";\n'
        "events {}\n"
        "http {\n"
        "    access_log off;\n"
        f'    include "{CONFIG}";\n'
        "}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [nginx, "-t", "-p", str(tmp_path), "-c", str(wrapper)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
