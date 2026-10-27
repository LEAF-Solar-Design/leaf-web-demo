"""Bounded project page data and portable, deterministic Next.js source export."""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile


_TEXT_LIMITS = {"label": (1, 120), "headline": (1, 240),
                "description": (0, 1200), "status": (0, 80)}
_ENUMS = {"theme": ("cream", "light", "dark"), "alignment": ("center", "left")}
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\b(?:https?|ftp|file|data|javascript):|\bwww\.)", re.I)
MAX_DOCUMENT_BYTES = 8192
PACKAGE_VERSIONS = {"next": "16.4.3", "react": "18.3.1", "react-dom": "18.3.1"}

# Shared visual contract. The Studio preview uses these same classes and values.
PAGE_CSS = """.project-page {
  --page-bg: #faf5ee;
  --page-fg: #463027;
  --page-muted: #755d4e;
  box-sizing: border-box;
  min-height: 100%;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: clamp(24px, 6vw, 72px);
  background: var(--page-bg);
  color: var(--page-fg);
  font-family: Georgia, 'Times New Roman', serif;
  text-align: center;
}
.project-page[data-theme="light"] {
  --page-bg: #ffffff;
  --page-fg: #202020;
  --page-muted: #666666;
}
.project-page[data-theme="dark"] {
  --page-bg: #201c1a;
  --page-fg: #faf5ee;
  --page-muted: #c4b99f;
}
.project-page[data-alignment="left"] { text-align: left; }
.project-page__content { width: min(100%, 48rem); min-width: 0; }
.project-page__label, .project-page__headline,
.project-page__description, .project-page__status {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.project-page__label {
  margin: 0 0 2rem;
  font-family: Arial, sans-serif;
  font-size: .875rem;
  line-height: 1.6;
  letter-spacing: .12em;
}
.project-page__headline {
  margin: 0;
  font-weight: 400;
  font-size: clamp(2.5rem, 8vw, 5.5rem);
  line-height: 1.08;
}
.project-page__description {
  margin: 1.75rem 0 0;
  font-size: clamp(1rem, 2.5vw, 1.25rem);
  line-height: 1.7;
  color: var(--page-muted);
}
.project-page__status {
  margin: 2.5rem 0 0;
  font-family: Arial, sans-serif;
  font-size: .875rem;
  line-height: 1.6;
  color: var(--page-muted);
}
"""

_PAGE_JSX = """import page from '../page.json';

export default function Page() {
  return (
    <main className="project-page" data-theme={page.theme} data-alignment={page.alignment}>
      <div className="project-page__content">
        <p className="project-page__label">{page.label}</p>
        <h1 className="project-page__headline">{page.headline}</h1>
        {page.description && <p className="project-page__description">{page.description}</p>}
        {page.status && <p className="project-page__status">{page.status}</p>}
      </div>
    </main>
  );
}
"""
_LAYOUT_JSX = """import './globals.css';
import page from '../page.json';

export const metadata = { title: page.label, description: page.description };

export default function RootLayout({ children }) {
  return <html lang="en"><body>{children}</body></html>;
}
"""
_README = """# Your coming-soon page

This is an independent Next.js App Router project. It requires Node.js 20.9 or
newer and npm. It has no Studio endpoint, token, account, or license dependency.
Saving a draft in Studio does not publish this page.

## Run locally

Extract the ZIP into a new directory, then run:

```sh
npm install
npm run build
npm start
```

Open http://localhost:3000. For development use `npm run dev`.
The direct Next.js and React versions are pinned in package.json. `npm install`
creates package-lock.json; retain it with your project and use `npm ci` for
repeatable subsequent installs. This export does not claim a completed build.

## Change the page

Edit page.json for text, theme (cream, light, dark), and alignment (center, left).
Text remains React data, not HTML. The layout is in app/page.jsx and styles are
in app/globals.css. No external fonts or image services are required.

## Publish using your own Vercel account

Put the extracted project in a repository you own, including the package lock
created during installation. In your Vercel account, import that repository,
select Next.js, and use the directory containing package.json as the root.
Use `npm run build` and the framework's default output setting. Deploy only
when you choose to publish. Review your account's plan and billing settings.
This export does not create or connect accounts, deploy, change domains, or
modify any existing Vercel site or QR code. It needs no environment secrets.

## Source identity

source-manifest.json records the saved revision, canonical document SHA256,
and SHA256 plus byte count of every other file in this archive. The manifest
does not hash itself. These hashes identify the exported source; they are not
a build, provider, or publication receipt.
"""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_document(document: dict) -> dict:
    """Return a detached canonical document, or raise ValueError.

    Only literal text and the two closed preset enums enter the model. There
    is no field for executable source, HTML, CSS, links, or resource fetching.
    Text is preserved exactly, including newlines; no silent trimming occurs.
    """
    if type(document) is dict and 'schema' in document:
        return _portfolio_template().validate_document(document)
    if type(document) is not dict or set(document) != set(_TEXT_LIMITS) | set(_ENUMS):
        raise ValueError("Page must contain exactly the six supported fields")
    for field, (minimum, maximum) in _TEXT_LIMITS.items():
        value = document[field]
        if type(value) is not str or not minimum <= len(value) <= maximum:
            raise ValueError(f"Invalid {field} length")
        if minimum and not value.strip():
            raise ValueError(f"{field} must contain text")
        if any(char != "\n" and unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
            raise ValueError(f"Control characters are not allowed in {field}")
        if "<" in value or ">" in value or _URL.search(value):
            raise ValueError(f"Use plain text without HTML or URLs in {field}")
    for field, choices in _ENUMS.items():
        if type(document[field]) is not str or document[field] not in choices:
            raise ValueError(f"Invalid {field} preset")
    result = {key: document[key] for key in sorted(document)}
    if len(_canonical_json(result)) > MAX_DOCUMENT_BYTES:
        raise ValueError("Page document is too large")
    return result


def document_digest(document: dict) -> str:
    """SHA256 of canonical UTF-8 JSON, independent of input key order."""
    return hashlib.sha256(_canonical_json(validate_document(document))).hexdigest()


def _portfolio_template():
    # Also support the dependency-free static suite's direct module loading.
    if __package__:
        from . import portfolio_page_template
        return portfolio_page_template
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('portfolio_page_template', Path(__file__).with_name('portfolio_page_template.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_archive(document: dict, revision: int, previews=None) -> bytes:
    """Export a deterministic ZIP with no user data interpolated into source."""
    page = validate_document(document)
    if page.get('schema') == 'leaf.portfolio.v1':
        return _portfolio_template().source_archive(page, revision, previews or {}, PACKAGE_VERSIONS)
    if type(revision) is not int or not 1 <= revision <= 9007199254740991:
        raise ValueError("Revision must be a positive safe integer")
    package = {"name": "coming-soon-page", "version": "1.0.0", "private": True,
               "scripts": {"dev": "next dev", "build": "next build --webpack", "start": "next start"},
               "engines": {"node": ">=20.9.0"}, "dependencies": dict(PACKAGE_VERSIONS)}
    files = {
        "package.json": _canonical_json(package) + b"\n",
        "page.json": _canonical_json(page) + b"\n",
        "app/page.jsx": _PAGE_JSX.encode("utf-8"),
        "app/layout.jsx": _LAYOUT_JSX.encode("utf-8"),
        "app/globals.css": ("html, body { margin: 0; min-height: 100%; }\n"
                            "body > .project-page { min-height: 100vh; min-height: 100dvh; }\n" + PAGE_CSS).encode("utf-8"),
        "next.config.mjs": b"export default {};\n",
        ".gitignore": b"node_modules/\n.next/\nout/\n.env*\nnpm-debug.log*\n",
        "README.md": _README.encode("utf-8"),
    }
    manifest = {"schema": "leaf.project-page-source.v1", "revision": revision,
                "document_sha256": document_digest(page),
                "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                          for path, data in sorted(files.items())}}
    files["source-manifest.json"] = _canonical_json(manifest) + b"\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()
