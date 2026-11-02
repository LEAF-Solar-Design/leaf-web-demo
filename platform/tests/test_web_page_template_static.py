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
                                          "app/globals.css", "app/design.js", "next.config.mjs", ".gitignore", "README.md",
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
            self.assertTrue(css.endswith(template.PAGE_CSS + template.DESIGN_CSS))
            self.assertEqual(archive.read("app/design.js").decode(), template.DESIGN_JS)
            for color in ["#faf5ee", "#463027", "#755d4e", "#ffffff", "#202020", "#666666", "#201c1a", "#c4b99f"]:
                self.assertIn(color, css)
            self.assertIn('data-alignment="left"', css)
            self.assertIn("white-space: pre-wrap", css)
            self.assertIn("overflow-wrap: anywhere", css)
            self.assertIn("100dvh", css)


def portfolio_document():
    preview = b'optimized preview fixture'
    return {
        'schema': 'leaf.portfolio.v1', 'name': 'Jessica Sargent', 'headline': 'Selected work',
        'introduction': 'Cakes and pastry', 'about': 'Made with care.',
        'contact': {'email': 'hello@example.com', 'location': 'Portland'},
        'resume': [{'heading': 'Experience', 'entries': [{'title': 'Pastry chef', 'subtitle': 'Kitchen', 'dates': '2024', 'description': 'Baking'}]}],
        'theme': 'light', 'categories': [{'slug': 'cakes', 'title': 'Cakes', 'description': '', 'hero': 0,
            'images': [{'material_id': '11111111-1111-4111-8111-111111111111', 'sha256': 'a' * 64,
                        'preview_sha256': hashlib.sha256(preview).hexdigest(), 'title': 'A cake', 'alt': 'Chocolate cake'}]}],
    }


class PortfolioTemplateTests(unittest.TestCase):
    def test_deep_detach_and_exact_nested_shape(self):
        original = portfolio_document()
        detached = template.validate_document(original)
        detached['categories'][0]['images'][0]['title'] = 'Changed'
        self.assertEqual(original['categories'][0]['images'][0]['title'], 'A cake')
        paths = [(), ('contact',), ('categories', 0), ('categories', 0, 'images', 0),
                 ('resume', 0), ('resume', 0, 'entries', 0)]
        for path in paths:
            value = portfolio_document()
            target = value
            for key in path:
                target = target[key]
            target['unknown'] = 'value'
            with self.subTest(path=path), self.assertRaises(ValueError):
                template.validate_document(value)

    def test_bad_schema_refs_hero_slug_and_prose(self):
        mutations = [
            lambda d: d.update(schema='leaf.portfolio.import.v1'),
            lambda d: d.update(theme='dark'),
            lambda d: d['categories'][0].update(hero=True),
            lambda d: d['categories'][0].update(hero=1),
            lambda d: d['categories'][0].update(slug='about'),
            lambda d: d['categories'][0].update(slug='../cakes'),
            lambda d: d['categories'][0]['images'][0].update(material_id='../../secret'),
            lambda d: d['categories'][0]['images'][0].update(sha256='A' * 64),
            lambda d: d['categories'][0]['images'][0].update(alt=''),
            lambda d: d['categories'][0]['images'].append(dict(d['categories'][0]['images'][0])),
            lambda d: d['contact'].update(email='a@b.com\n'),
            lambda d: d['contact'].update(email='javascript:alert(1)'),
            lambda d: d.update(about='<script>bad</script>'),
            lambda d: d.update(introduction='https://example.com'),
            lambda d: d['resume'][0]['entries'][0].update(description='bad\u202e'),
        ]
        for mutate in mutations:
            value = portfolio_document()
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                template.validate_document(value)

    def test_item_and_document_bounds(self):
        for mutate in [lambda d: d.update(categories=[]), lambda d: d.update(resume=d['resume'] * 9),
                       lambda d: d['resume'][0].update(entries=d['resume'][0]['entries'] * 21),
                       lambda d: d['categories'][0].update(images=[])]:
            value = portfolio_document(); mutate(value)
            with self.assertRaises(ValueError):
                template.validate_document(value)
        value = portfolio_document()
        # All individual fields fit; the aggregate canonical UTF-8 limit fails.
        entry = dict(title='x' * 200, subtitle='x' * 240, dates='x' * 120, description='🍰' * 2400)
        value['resume'] = [{'heading': 'Experience', 'entries': [dict(entry) for _ in range(20)]} for _ in range(8)]
        with self.assertRaisesRegex(ValueError, '512 KiB'):
            template.validate_document(value)

    def test_category_reuse_counts_toward_total_and_still_requires_unique_ids(self):
        value = portfolio_document()
        image = value['categories'][0]['images'][0]
        value['categories'][0]['images'] = [dict(image, material_id=f'00000000-0000-0000-0000-{index:012x}') for index in range(200)]
        self.assertEqual(len(template.validate_document(value)['categories'][0]['images']), 200)
        value['categories'].append(dict(slug='breads', title='Breads', description='', hero=0, images=[dict(image)]))
        with self.assertRaisesRegex(ValueError, '200 images'):
            template.validate_document(value)
        value['categories'][0]['images'] = [dict(image)]
        self.assertEqual(len(template.validate_document(value)['categories']), 2)
        value['categories'][1]['slug'] = 'cakes'
        with self.assertRaises(ValueError):
            template.validate_document(value)

    def test_source_routes_assets_manifest_and_literal_data(self):
        value = portfolio_document()
        value['about'] = 'A "quote", `literal`, {braces}, \\ and Café'
        mid = value['categories'][0]['images'][0]['material_id']
        previews = {mid: b'optimized preview fixture'}
        first = template.source_archive(value, 3, previews)
        self.assertEqual(first, template.source_archive(value, 3, previews))
        self.assertNotEqual(first, template.source_archive(value, 4, previews))
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            manifest = json.loads(archive.read('source-manifest.json'))
            self.assertEqual(manifest['document_sha256'], template.document_digest(value))
            self.assertEqual(manifest['revision'], 3)
            self.assertEqual(set(manifest['files']), set(archive.namelist()) - {'source-manifest.json'})
            for path, record in manifest['files'].items():
                raw = archive.read(path)
                self.assertEqual(record, {'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)})
            for route in ['app/page.jsx', 'app/[slug]/page.jsx', 'app/about/page.jsx', 'app/resume/page.jsx']:
                source = archive.read(route).decode()
                self.assertNotIn(value['about'], source)
                self.assertNotIn('dangerouslySetInnerHTML', source)
            self.assertIn('generateStaticParams', archive.read('app/[slug]/page.jsx').decode())
            self.assertIn('Escape', archive.read('app/Gallery.jsx').decode())
            self.assertEqual(archive.read(f'public/images/{mid}.jpg'), previews[mid])
            asset = json.loads(archive.read('asset-manifest.json'))[mid]
            self.assertEqual(asset['sha256'], 'a' * 64)
            self.assertEqual(asset['preview_sha256'], hashlib.sha256(previews[mid]).hexdigest())
            self.assertEqual(json.loads(archive.read('portfolio.json')), value)
            self.assertEqual(json.loads(archive.read('package.json'))['dependencies'], template.PACKAGE_VERSIONS)
        for bad in [{}, {mid: b'wrong preview'}, {mid: previews[mid], 'foreign': b'bytes'}]:
            with self.assertRaises(ValueError):
                template.source_archive(value, 3, bad)

    def test_preview_and_archive_bounds_fail_before_output(self):
        module = template._portfolio_template()
        value = portfolio_document()
        previews = {value['categories'][0]['images'][0]['material_id']: b'optimized preview fixture'}
        module.MAX_PREVIEW_BYTES = 1
        with self.assertRaisesRegex(ValueError, '64 MiB'):
            module.source_archive(value, 1, previews, template.PACKAGE_VERSIONS)
        module.MAX_PREVIEW_BYTES = 64 * 1024 * 1024
        module.MAX_ARCHIVE_BYTES = 1
        with self.assertRaisesRegex(ValueError, '80 MiB'):
            module.source_archive(value, 1, previews, template.PACKAGE_VERSIONS)


def portfolio_producer(filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT / 'platform' / (filename + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortfolioLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = portfolio_producer('portfolio_page_template')
        cls.native = portfolio_producer('portfolio_ios_template')

    def test_legacy_canonical_bytes_and_digest_remain_unchanged(self):
        value = portfolio_document()
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        for producer, canonical in [(self.web, self.web.canonical), (self.native, self.native._canonical_json)]:
            with self.subTest(producer=producer.__name__):
                actual = producer.validate_document(value)
                self.assertNotIn('galleryLayout', actual)
                self.assertEqual(actual, value)
                self.assertIsNot(actual, value)
                self.assertEqual(canonical(actual), raw)
                self.assertEqual(hashlib.sha256(canonical(actual)).hexdigest(), digest)
        self.assertEqual(self.native.document_digest(value), digest)
        self.assertEqual(template.document_digest(value), digest)

    def test_both_validators_keep_exact_required_shape_and_layout_enum(self):
        for producer in [self.web, self.native]:
            for layout in ['grid', 'single']:
                value = dict(portfolio_document(), galleryLayout=layout)
                with self.subTest(producer=producer.__name__, layout=layout):
                    self.assertEqual(producer.validate_document(value), value)
            for layout in [None, True, False, 0, 1, [], {}, '', 'Grid', 'Single column', 'single\n']:
                with self.subTest(producer=producer.__name__, invalid=layout), self.assertRaises(ValueError):
                    producer.validate_document(dict(portfolio_document(), galleryLayout=layout))
            for field in portfolio_document():
                value = dict(portfolio_document(), galleryLayout='single')
                del value[field]
                with self.subTest(producer=producer.__name__, missing=field), self.assertRaises(ValueError):
                    producer.validate_document(value)
            for path in [(), ('contact',), ('categories', 0), ('categories', 0, 'images', 0)]:
                value = portfolio_document()
                target = value
                for key in path:
                    target = target[key]
                target['unknown' if not path else 'galleryLayout'] = 'single'
                with self.subTest(producer=producer.__name__, path=path), self.assertRaises(ValueError):
                    producer.validate_document(value)

    def test_real_archives_preserve_layout_photos_and_renderer_branches(self):
        for native, producer in [(False, self.web), (True, self.native)]:
            previous_files = None
            for layout in [None, 'grid', 'single']:
                value = portfolio_document()
                first = value['categories'][0]['images'][0]
                second_bytes = b'second optimized preview fixture'
                second = dict(first, material_id='22222222-2222-4222-8222-222222222222',
                              sha256='b' * 64, preview_sha256=hashlib.sha256(second_bytes).hexdigest(),
                              title='Bread', alt='A loaf of bread')
                value['categories'][0]['images'].append(second)
                value['categories'][0]['hero'] = 1
                if layout is not None:
                    value['galleryLayout'] = layout
                previews = {first['material_id']: b'optimized preview fixture', second['material_id']: second_bytes}
                args = (value, 7, previews) if native else (value, 7, previews, template.PACKAGE_VERSIONS)
                with self.subTest(native=native, layout=layout):
                    result = producer.source_archive(*args)
                    self.assertEqual(result, producer.source_archive(*args))
                    with zipfile.ZipFile(io.BytesIO(result)) as archive:
                        files = {name: archive.read(name) for name in archive.namelist()}
                    data_path = 'Portfolio/PortfolioContent/portfolio.json' if native else 'portfolio.json'
                    self.assertEqual(json.loads(files[data_path]), value)
                    manifest = json.loads(files['source-manifest.json'])
                    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
                    self.assertEqual(files[data_path], raw + b'\n')
                    self.assertEqual(manifest['document_sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(manifest['revision'], 7)
                    self.assertEqual(set(manifest['files']), set(files) - {'source-manifest.json'})
                    for path, record in manifest['files'].items():
                        self.assertEqual(record, {'sha256': hashlib.sha256(files[path]).hexdigest(), 'size_bytes': len(files[path])})
                    assets = manifest['assets'] if native else json.loads(files['asset-manifest.json'])
                    self.assertEqual(set(assets), set(previews))
                    for image in value['categories'][0]['images']:
                        asset = assets[image['material_id']]
                        self.assertEqual(files[asset['path']], previews[image['material_id']])
                        self.assertEqual(asset['sha256'], image['sha256'])
                        self.assertEqual(asset['preview_sha256'], image['preview_sha256'])
                    if previous_files is not None:
                        for path in files:
                            if path not in (data_path, 'source-manifest.json'):
                                self.assertEqual(files[path], previous_files[path], path)
                    previous_files = files
                    if native:
                        swift = files['Portfolio/PortfolioApp.swift'].decode()
                        self.assertIn('let galleryLayout: String?', swift)
                        home, gallery = swift.split('struct GalleryView: View', 1)
                        self.assertIn('let legacyCount = adaptive ? max(1, Int((width + horizontalGap) / (240 + horizontalGap))) : 1', home)
                        self.assertIn('columns: document.appearance?.columns', home)
                        self.assertIn('galleryLayout == "single"', gallery)
                        self.assertIn('category.layout?.columns ?? document.appearance?.columns ?? (document.galleryLayout == "single" ? 1 : nil)', gallery)
                        self.assertIn('adaptive: true', gallery)
                        self.assertIn('DesignGrid(columns:', gallery)
                    else:
                        route = files['app/[slug]/page.jsx'].decode()
                        self.assertIn('<Gallery page={page} category={c}', route)
                        viewer = files['app/Gallery.jsx'].decode()
                        self.assertIn('Gallery({page,category})', viewer)
                        self.assertIn("data-gallery-layout={page.galleryLayout||'grid'}", viewer)
                        self.assertIn("c?.layout?.columns??page.appearance?.columns??(c&&page.galleryLayout==='single'?1:2)", files['app/design.js'].decode())
                        self.assertNotIn('data-gallery-layout', files['app/page.jsx'].decode())
                        css = files['app/globals.css'].decode()
                        self.assertIn('.portfolio .portfolio-grid[data-gallery-layout="single"]{grid-template-columns:minmax(0,1fr)}', css)
                        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))', css)
                        self.assertIn('@media(max-width:600px){.portfolio-grid{grid-template-columns:minmax(0,1fr)}', css)


class EnvironmentDesignSchemaTests(unittest.TestCase):
    def test_portfolio_optional_families_roundtrip_without_default_injection(self):
        value = portfolio_document()
        value['appearance'] = {'font': 'mono', 'columns': 4, 'mobileColumns': 2, 'palette': {'background': '#AbCdEF'}}
        value['textStyles'] = {'headline': {'size': 96, 'lineHeight': 2.5, 'letterSpacing': -1, 'italic': True}}
        value['nav'] = {'order': ['resume', 'home'], 'hidden': True, 'appearance': {'color': '#ABCDEF'}}
        value['labels'] = {'aboutHeading': 'About me', 'viewerClose': 'Back'}
        value['categories'][0].update(hidden=True, navLabel='Bakes', layout={'hidden': True, 'gap': 0},
                                      textStyles={'navigation': {'size': 12}})
        value['categories'][0]['images'][0].update(display={'fit': 'cover', 'focal': 'bottom-right', 'span': 2},
                                                  captionStyle={'underline': True})
        value['resume'] = [{'heading': 'Experience', 'headingStyle': {'font': 'serif'}, 'layout': {'padding': 96},
                            'entries': [{'title': 'Chef', 'subtitle': '', 'dates': '', 'description': '',
                                         'layout': {'columns': 2}, 'textStyles': {'title': {'weight': 'bold'}}}]}]
        for name in ('portfolio_page_template', 'portfolio_ios_template', 'web_page_template'):
            producer = portfolio_producer(name)
            with self.subTest(producer=name):
                result = producer.validate_document(value)
                self.assertEqual(result, value)
                result['appearance']['palette']['background'] = '#000000'
                self.assertEqual(value['appearance']['palette']['background'], '#AbCdEF')

    def test_optional_fields_reject_unknown_malformed_and_unsafe_values(self):
        cases = [('appearance', {}), ('appearance', {'columns': True}), ('appearance', {'columns': 1.5}),
                 ('appearance', {'columns': 5}), ('appearance', {'mobileColumns': 3}),
                 ('appearance', {'palette': {}}), ('appearance', {'palette': {'text': '#fff'}}),
                 ('appearance', {'arbitraryCSS': 'display:none'}), ('textStyles', {'headline': {}}),
                 ('textStyles', {'headline': {'size': None}}), ('textStyles', {'headline': {'size': False}}),
                 ('textStyles', {'headline': {'italic': False}}), ('textStyles', {'headline': {'lineHeight': float('inf')}}),
                 ('textStyles', {'missing': {'size': 12}}), ('nav', {'order': ['home', 'home']}),
                 ('nav', {'order': ['external']}), ('nav', {'appearance': {'hidden': True}}),
                 ('labels', {'viewerClose': 'https://example.com'}), ('labels', {'aboutHeading': None})]
        for name in ('portfolio_page_template', 'portfolio_ios_template', 'web_page_template'):
            producer = portfolio_producer(name)
            for key, config in cases:
                value = portfolio_document()
                value[key] = config
                with self.subTest(producer=name, key=key, config=config), self.assertRaises(ValueError):
                    producer.validate_document(value)

    def test_plain_page_optional_data_is_detached_and_bounded(self):
        value = document(appearance={'font': 'mono', 'padding': 0, 'palette': {'accent': '#ABCDEF'}},
                         textStyles={'status': {'hidden': True}, 'headline': {'size': 96}})
        result = template.validate_document(value)
        self.assertEqual(result, value)
        result['appearance']['font'] = 'serif'
        self.assertEqual(value['appearance']['font'], 'mono')
        for config in ({}, {'align': 'left'}, {'padding': True}, {'padding': 97}, {'palette': None}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                template.validate_document(document(appearance=config))

    def test_configured_next_source_is_data_independent_and_keeps_original_bindings(self):
        producer = portfolio_producer('portfolio_page_template')
        value = portfolio_document()
        image = value['categories'][0]['images'][0]
        previews = {image['material_id']: b'optimized preview fixture'}
        configured = json.loads(json.dumps(value))
        configured['appearance'] = {'columns': 4, 'font': 'mono', 'palette': {'background': '#123456'}}
        configured['categories'][0]['images'][0]['display'] = {'fit': 'cover', 'focal': 'top-left'}
        archives = []
        for page in (value, configured):
            with zipfile.ZipFile(io.BytesIO(producer.source_archive(page, 7, previews, template.PACKAGE_VERSIONS))) as archive:
                archives.append({path: archive.read(path) for path in archive.namelist()})
        for path in archives[0]:
            if path not in ('portfolio.json', 'source-manifest.json'):
                self.assertEqual(archives[0][path], archives[1][path], path)
        self.assertEqual(json.loads(archives[1]['portfolio.json']), configured)
        self.assertIn(b'photoStyle(image.display,page.appearance)', archives[1]['app/Gallery.jsx'])
        self.assertIn(b'textStyle(e.textStyles?.title)', archives[1]['app/resume/page.jsx'])



    def test_configured_native_archive_consumes_design_and_is_data_independent(self):
        producer = portfolio_producer('portfolio_ios_template')
        original = portfolio_document()
        configured = json.loads(json.dumps(original))
        configured['appearance'] = {'font': 'mono', 'scale': 'large', 'headingWeight': 'bold',
                                    'spacing': 'airy', 'width': 'wide', 'align': 'right',
                                    'columns': 4, 'mobileColumns': 2, 'gap': 0, 'padding': 8,
                                    'maxWidth': 900, 'imageFit': 'cover', 'aspect': 'portrait',
                                    'focal': 'bottom-right', 'palette': {'background': '#ABCDEF',
                                    'text': '#123456', 'muted': '#234567', 'accent': '#345678'}}
        style = {'font': 'serif', 'size': 36, 'weight': 'medium', 'color': '#AbCdEf',
                 'align': 'center', 'lineHeight': 2, 'letterSpacing': 3,
                 'italic': True, 'underline': True}
        box = {'columns': 2, 'mobileColumns': 2, 'gap': 8, 'padding': 4,
               'maxWidth': 600, 'align': 'center', 'background': '#123456',
               'radius': 8, 'borderWidth': 2, 'borderColor': '#234567', 'shadow': 'strong'}
        configured['textStyles'] = {key: dict(style) for key in
            ('name', 'headline', 'introduction', 'about', 'aboutHeading', 'contactHeading', 'location', 'email', 'resumeHeading')}
        configured['labels'] = {'aboutHeading': 'About the baker', 'contactHeading': 'Get in touch',
                                'resumeHeading': 'Work history', 'viewerClose': 'Back to gallery'}
        configured['nav'] = {'order': ['resume', 'galleries', 'about', 'home'], 'layout': 'column',
                             'homeLabel': 'Work', 'aboutLabel': 'Contact', 'resumeLabel': 'Experience',
                             'appearance': dict(style), 'gap': 4, 'padding': 6, 'radius': 8, 'background': '#ABCDEF'}
        category = configured['categories'][0]
        category.update(layout=dict(box), navLabel='Gallery', textStyles={key: dict(style) for key in ('title', 'description', 'navigation')})
        category['images'][0].update(display={'fit': 'cover', 'aspect': 'square', 'focal': 'top-right', 'radius': 12, 'span': 2},
                                     captionStyle=dict(style))
        configured['resume'] = [{'heading': 'Experience', 'headingStyle': dict(style), 'layout': dict(box),
            'entries': [{'title': 'Chef', 'subtitle': 'Bakery', 'dates': '2026', 'description': 'Pastry',
                         'layout': dict(box), 'textStyles': {key: dict(style) for key in ('title', 'subtitle', 'dates', 'description')}}]}]
        mid = original['categories'][0]['images'][0]['material_id']
        files = []
        for value in (original, configured):
            with zipfile.ZipFile(io.BytesIO(producer.source_archive(value, 7, {mid: b'optimized preview fixture'}))) as archive:
                files.append({path: archive.read(path) for path in archive.namelist()})
        data_path = 'Portfolio/PortfolioContent/portfolio.json'
        self.assertEqual(json.loads(files[1][data_path]), configured)
        for path in files[0]:
            if path not in (data_path, 'source-manifest.json'):
                self.assertEqual(files[0][path], files[1][path], path)
        swift = files[1]['Portfolio/PortfolioApp.swift'].decode()
        # These pin real consumer sites, not just optional Decodable declarations.
        for consumer in ('style?.size', 'style?.font', 'style?.weight', 'style?.color',
                         'style?.align', 'style?.lineHeight', 'style?.letterSpacing',
                         'style?.italic', 'style?.underline', 'style?.hidden',
                         'value?.padding', 'value?.maxWidth', 'value?.background', 'value?.radius',
                         'value?.borderColor', 'value?.borderWidth', 'value?.shadow',
                         'photo.display?.fit', 'photo.display?.aspect', 'photo.display?.focal',
                         'photo.display?.span', 'photo.display?.radius', 'photo.display?.hidden',
                         'nav.groups', 'nav.layout', 'nav.gap', 'nav.padding', 'nav.radius',
                         'nav.background', 'nav.homeLabel', 'nav.aboutLabel', 'nav.resumeLabel',
                         'nav.hideHome', 'nav.hideAbout', 'nav.hideResume', 'nav.hidden',
                         'category.textStyles?', 'section.headingStyle', 'entry.textStyles?',
                         'document.labels?["viewerClose"]', 'document.appearance?.mobileColumns'):
            self.assertIn(consumer, swift)
        self.assertIn('@ScaledMetric(relativeTo: .body)', swift)
        self.assertNotIn('.dynamicTypeSize(', swift)
        self.assertIn('Text(verbatim: value)', swift)
        self.assertIn('case "top-right": return .topTrailing', swift)
        self.assertIn('case "bottom-left": return .bottomLeading', swift)
        self.assertIn('view[PhotoSpan.self]', swift)
        self.assertIn('layoutValue(key: PhotoSpan.self', swift)
        self.assertEqual(configured['categories'][0]['images'][0]['sha256'], original['categories'][0]['images'][0]['sha256'])

    def test_nested_design_bounds_and_closed_keys_agree_across_all_producers(self):
        cases = [
            (('categories', 0, 'layout'), {'gap': -1}),
            (('categories', 0, 'layout'), {'maxWidth': 1801}),
            (('categories', 0, 'layout'), {'padding': False}),
            (('categories', 0, 'layout'), {'imageFit': 'cover'}),
            (('categories', 0, 'images', 0, 'display'), {'focal': 'north'}),
            (('categories', 0, 'images', 0, 'display'), {'span': 3}),
            (('categories', 0, 'images', 0, 'display'), {'hidden': False}),
            (('categories', 0, 'images', 0, 'captionStyle'), {'lineHeight': float('nan')}),
            (('categories', 0, 'images', 0, 'captionStyle'), {'letterSpacing': -2}),
            (('categories', 0, 'images', 0, 'captionStyle'), {'size': 97}),
        ]
        for name in ('portfolio_page_template', 'portfolio_ios_template', 'web_page_template'):
            producer = portfolio_producer(name)
            for path, invalid in cases:
                value = portfolio_document()
                node = value
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = invalid
                with self.subTest(producer=name, path=path, invalid=invalid), self.assertRaises(ValueError):
                    producer.validate_document(value)


if __name__ == '__main__':
    unittest.main()
