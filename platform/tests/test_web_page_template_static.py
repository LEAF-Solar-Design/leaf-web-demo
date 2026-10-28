"""Dependency-free checks for the bounded page document and Next source export.

Run directly with Python; unittest also makes these discoverable by pytest.
"""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("web_page_template", ROOT / "platform/web_page_template.py")
template = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(template)


def document(**changes):
    return dict(label="Jessica's baking", headline="Something sweet\nis on the way.",
                description="A collection of bakes, made with care.", status="Coming soon",
                theme="cream", alignment="center", **changes)


class WebPageTemplateTests(unittest.TestCase):
    def test_canonical_document_is_detached_and_exact(self):
        original = document()
        original["description"] = "  Café 🍰\nMade with care.  "
        actual = template.validate_document(original)
        self.assertEqual(actual, original)
        self.assertIsNot(actual, original)
        self.assertEqual(list(actual), sorted(actual))
        actual["headline"] = "Changed"
        self.assertNotEqual(original["headline"], actual["headline"])

    def test_required_exact_shape(self):
        bad = [None, [], "{}", {}, dict(document(), extra="field")]
        for field in document():
            missing = document()
            del missing[field]
            bad.append(missing)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                template.validate_document(value)

    def test_text_type_and_length_boundaries(self):
        for field, lower, upper in [("label", 1, 120), ("headline", 1, 240),
                                     ("description", 0, 1200), ("status", 0, 80)]:
            for length in [lower, upper]:
                value = document()
                value[field] = "x" * length
                self.assertEqual(template.validate_document(value)[field], value[field])
            for content in [None, True, 42, [], {}, "x" * (upper + 1)] + (["", " \n "] if lower else []):
                value = document()
                value[field] = content
                with self.subTest(field=field, content=content), self.assertRaises(ValueError):
                    template.validate_document(value)

    def test_rejects_controls_html_and_urls(self):
        examples = ["hello\x00", "hello\t", "hello\r\n", "hello\x7f", "hello\u0085",
                    "hello\u202e", "hello\ud800", "<script>alert(1)</script>",
                    "<style>body{display:none}</style>", "https://example.com", "HTTP:example.com",
                    "www.example.com", "javascript:alert(1)", "data:text/html,hello",
                    "file:///etc/passwd", "custom://address"]
        for content in examples:
            value = document()
            value["description"] = content
            with self.subTest(content=repr(content)), self.assertRaises(ValueError):
                template.validate_document(value)

    def test_presets_are_closed(self):
        for theme in ["cream", "light", "dark"]:
            for alignment in ["center", "left"]:
                value = dict(document(), theme=theme, alignment=alignment)
                self.assertEqual(template.validate_document(value), value)
        for field in ["theme", "alignment"]:
            for content in [None, True, [], "Cream", "", "center; color:red"]:
                with self.subTest(field=field, content=content), self.assertRaises(ValueError):
                    template.validate_document(dict(document(), **{field: content}))

    def test_digest_is_canonical_utf8_and_content_bound(self):
        value = document()
        value["description"] = "Café 🍰"
        expected = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(template.document_digest(value), expected)
        self.assertEqual(template.document_digest(dict(reversed(list(value.items())))), expected)
        self.assertNotEqual(template.document_digest(dict(value, headline="Changed")), expected)
        with self.assertRaises(ValueError):
            template.document_digest(dict(value, unexpected="value"))

    def test_archive_is_reproducible_and_revision_bound(self):
        value = document()
        first = template.source_archive(value, 1)
        self.assertEqual(template.source_archive(dict(reversed(list(value.items()))), 1), first)
        self.assertNotEqual(template.source_archive(value, 2), first)
        for revision in [0, -1, True, 1.0, "1", None, 9007199254740992]:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                template.source_archive(value, revision)

    def test_manifest_binds_every_other_file_and_exact_document(self):
        value = document()
        with zipfile.ZipFile(io.BytesIO(template.source_archive(value, 7))) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(set(names), {"package.json", "page.json", "app/page.jsx", "app/layout.jsx",
                                          "app/globals.css", "next.config.mjs", ".gitignore", "README.md",
                                          "source-manifest.json"})
            manifest = json.loads(archive.read("source-manifest.json"))
            self.assertEqual(manifest["schema"], "leaf.project-page-source.v1")
            self.assertEqual(manifest["revision"], 7)
            self.assertEqual(manifest["document_sha256"], template.document_digest(value))
            self.assertEqual(set(manifest["files"]), set(names) - {"source-manifest.json"})
            for path, record in manifest["files"].items():
                data = archive.read(path)
                self.assertEqual(record, {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
            self.assertEqual(json.loads(archive.read("page.json")), value)
            for info in archive.infolist():
                self.assertFalse(info.filename.startswith("/"))
                self.assertNotIn("..", info.filename.split("/"))
                self.assertEqual(info.external_attr >> 16, 0o100644)

    def test_customer_text_only_changes_data_and_manifest(self):
        value = document()
        value["description"] = "She said: \"It's ready!\"\n`literal` {text} \\ Café"
        with zipfile.ZipFile(io.BytesIO(template.source_archive(document(), 1))) as before:
            with zipfile.ZipFile(io.BytesIO(template.source_archive(value, 1))) as after:
                for path in before.namelist():
                    if path not in ("page.json", "source-manifest.json"):
                        self.assertEqual(before.read(path), after.read(path), path)
                jsx = after.read("app/page.jsx").decode()
                self.assertIn("import page from '../page.json'", jsx)
                self.assertIn("{page.headline}", jsx)
                self.assertNotIn("dangerouslySetInnerHTML", jsx)
                self.assertEqual(json.loads(after.read("page.json")), value)

    def test_fixed_next_package_has_independent_commands(self):
        runtime = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
        with zipfile.ZipFile(io.BytesIO(template.source_archive(document(), 1))) as archive:
            package = json.loads(archive.read("package.json"))
            expected = {name: runtime["dependencies"][name] for name in ["next", "react", "react-dom"]}
            self.assertEqual(package["dependencies"], expected)
            self.assertEqual(package["scripts"], {"dev": "next dev", "build": "next build --webpack", "start": "next start"})
            self.assertNotIn("devDependencies", package)
            readme = archive.read("README.md").decode()
            for instruction in ["npm install", "npm run build", "npm start", "your Vercel account"]:
                self.assertIn(instruction, readme)

    def test_export_uses_shared_preset_css(self):
        with zipfile.ZipFile(io.BytesIO(template.source_archive(document(), 1))) as archive:
            css = archive.read("app/globals.css").decode()
            self.assertTrue(css.endswith(template.PAGE_CSS))
            for color in ["#faf5ee", "#463027", "#755d4e", "#ffffff", "#202020", "#666666", "#201c1a", "#c4b99f"]:
                self.assertIn(color, css)
            self.assertIn('data-alignment="left"', css)
            self.assertIn("white-space: pre-wrap", css)
            self.assertIn("overflow-wrap: anywhere", css)
            self.assertIn("100dvh", css)


if __name__ == "__main__":
    unittest.main()
