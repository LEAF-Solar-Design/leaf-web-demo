"""Exercise the actual native CI bootstrap without touching host apt state."""
from pathlib import Path

import pytest


@pytest.fixture
def filter_sources():
    script = (Path(__file__).resolve().parents[1] / ".codebuild/ci.sh").read_text()
    code = script.split("python - <<'LEAF_CI_APT_SOURCE_FILTER'\n", 1)[1].split(
        "\nLEAF_CI_APT_SOURCE_FILTER", 1)[0]
    namespace = {"__name__": "ci_supply_fixture"}
    exec(compile(code, "ci.sh:apt-filter", "exec"), namespace)
    return namespace["disable_unused_chrome_sources"]


def write(root, name, contents):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents.encode())
    return path


def test_only_exact_chrome_lines_are_disabled(tmp_path, filter_sources):
    ubuntu = "deb http://archive.ubuntu.com/ubuntu jammy main\r\n"
    chrome = "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] https://dl.google.com/linux/chrome-stable/deb/ stable main\r\n"
    comment = "# deb https://dl.google.com/linux/chrome-stable/deb stable main\r\n"
    path = write(tmp_path, "sources.list", ubuntu + chrome + comment)
    filter_sources(tmp_path)
    expected = ubuntu + "# leaf-ci unused Chrome source: " + chrome + comment
    assert path.read_bytes() == expected.encode()
    filter_sources(tmp_path)
    assert path.read_bytes() == expected.encode()


@pytest.mark.parametrize("uri", [
    "https://dl.google.com.evil.test/linux/chrome-stable/deb",
    "https://dl.google.com/linux/chrome-stable/deb-extra",
    "https://dl.google.com/linux/chrome-stable/deb?x=1",
    "https://example.test/dl.google.com/linux/chrome-stable/deb",
])
def test_lookalikes_untouched(tmp_path, filter_sources, uri):
    original = f"deb {uri} stable main\n"
    path = write(tmp_path, "sources.list.d/other.list", original)
    filter_sources(tmp_path)
    assert path.read_bytes() == original.encode()


def test_missing_sources_noop(tmp_path, filter_sources):
    filter_sources(tmp_path)
    filter_sources(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_deb822_preserves_unrelated_and_mixed_uris(tmp_path, filter_sources):
    unrelated = "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: jammy\nComponents: main\n"
    mixed = "Types: deb\nURIs: https://dl.google.com/linux/chrome-stable/deb\n https://example.test/packages\nSuites: stable\nSigned-By: /keys/existing.gpg\n"
    path = write(tmp_path, "sources.list.d/mixed.sources", unrelated + "\n" + mixed)
    filter_sources(tmp_path)
    expected = unrelated + "\n" + mixed.replace(
        "URIs: https://dl.google.com/linux/chrome-stable/deb\n https://example.test/packages",
        "URIs: https://example.test/packages")
    assert path.read_bytes() == expected.encode()
    filter_sources(tmp_path)
    assert path.read_bytes() == expected.encode()


def test_deb822_target_only_is_disabled(tmp_path, filter_sources):
    original = "Types: deb\nURIs: http://dl.google.com/linux/chrome-stable/deb/\nSuites: stable\nComponents: main\n"
    path = write(tmp_path, "sources.list.d/google.sources", original)
    filter_sources(tmp_path)
    expected = "".join("# leaf-ci unused Chrome source: " + line for line in original.splitlines(keepends=True))
    assert path.read_bytes() == expected.encode()
    filter_sources(tmp_path)
    assert path.read_bytes() == expected.encode()
