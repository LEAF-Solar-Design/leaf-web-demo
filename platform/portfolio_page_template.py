"""Strict portable portfolio data and deterministic, local-only source exports."""
from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import uuid
import zipfile

MAX_DOCUMENT_BYTES = 512 * 1024
MAX_PREVIEW_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
_URL = re.compile(r'(?:[a-z][a-z0-9+.-]*://|\b(?:https?|ftp|file|data|javascript):|\bwww\.)', re.I)
_HEX = re.compile(r'[0-9a-f]{64}')
_EMAIL = re.compile(r'[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*@[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)+')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def exact(value, keys, optional=()):
    if type(value) is not dict or set(value) - set(optional) != set(keys.split()):
        raise ValueError('Unsupported portfolio fields')


def text(value, minimum, maximum):
    if (type(value) is not str or not minimum <= len(value) <= maximum
            or (minimum and not value.strip())
            or any(c != '\n' and unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)
            or '<' in value or '>' in value or _URL.search(value)):
        raise ValueError(f'Use plain text of {minimum} to {maximum} characters')


def items(value, minimum, maximum):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError('Invalid portfolio item count')


def validate_document(document):
    exact(document, 'schema name headline introduction about contact resume categories theme', ('galleryLayout',))
    if 'galleryLayout' in document and (type(document['galleryLayout']) is not str or document['galleryLayout'] not in ('grid', 'single')):
        raise ValueError('Unsupported photo layout')
    if document['schema'] != 'leaf.portfolio.v1' or document['theme'] != 'light':
        raise ValueError('Unsupported portfolio schema or theme')
    for key, low, high in [('name', 1, 120), ('headline', 1, 240), ('introduction', 0, 2400), ('about', 0, 8000)]:
        text(document[key], low, high)
    contact = document['contact']
    exact(contact, 'email location')
    email = contact['email']
    if type(email) is not str or len(email) > 254 or (email and not _EMAIL.fullmatch(email)):
        raise ValueError('Use a valid contact email or leave it empty')
    text(contact['location'], 0, 160)
    items(document['resume'], 0, 8)
    for section in document['resume']:
        exact(section, 'heading entries')
        text(section['heading'], 1, 120)
        items(section['entries'], 0, 20)
        for entry in section['entries']:
            exact(entry, 'title subtitle dates description')
            for key, high in [('title', 200), ('subtitle', 240), ('dates', 120), ('description', 2400)]:
                text(entry[key], 0, high)
    items(document['categories'], 1, 12)
    slugs, total = set(), 0
    references = {}
    for category in document['categories']:
        exact(category, 'slug title description hero images')
        slug = category['slug']
        if (type(slug) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slug)
                or slug in slugs or slug in {'about', 'resume', 'home'}):
            raise ValueError('Use unique category slugs, excluding home, about and resume')
        slugs.add(slug)
        text(category['title'], 1, 120)
        text(category['description'], 0, 1200)
        items(category['images'], 1, 200)
        total += len(category['images'])
        if total > 200 or type(category['hero']) is not int or not 0 <= category['hero'] < len(category['images']):
            raise ValueError('Invalid hero or more than 200 images')
        seen = set()
        for image in category['images']:
            exact(image, 'material_id sha256 preview_sha256 title alt')
            mid = image['material_id']
            try:
                valid = type(mid) is str and str(uuid.UUID(mid)) == mid
            except (ValueError, AttributeError):
                valid = False
            if not valid or mid in seen:
                raise ValueError('Invalid or duplicate photo reference')
            seen.add(mid)
            for key in ('sha256', 'preview_sha256'):
                if type(image[key]) is not str or not _HEX.fullmatch(image[key]):
                    raise ValueError('Invalid photo digest')
            pair = (image['sha256'], image['preview_sha256'])
            if mid in references and references[mid] != pair:
                raise ValueError('Conflicting photo digests')
            references[mid] = pair
            text(image['title'], 0, 200)
            text(image['alt'], 1, 400)
    raw = canonical(document)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError('Portfolio exceeds 512 KiB')
    return json.loads(raw)


def image_references(document):
    return {image['material_id']: image for category in document['categories'] for image in category['images']}


PORTFOLIO_CSS = """*{box-sizing:border-box}body{margin:0}.portfolio{background:white;color:#202020;font:16px/1.6 Arial,sans-serif;min-height:100%;padding:clamp(20px,4vw,64px);overflow-wrap:anywhere}.portfolio nav{display:flex;flex-wrap:wrap;gap:12px 24px;margin-bottom:48px}.portfolio button,.portfolio a{color:inherit;font:inherit}.portfolio nav button{background:none;border:0;padding:4px;cursor:pointer}.portfolio h1{font:400 clamp(32px,5vw,64px)/1.15 Georgia,serif}.portfolio p{white-space:pre-wrap;max-width:70ch}.portfolio-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:32px}.portfolio-grid button,.portfolio-grid a{display:block;min-width:0;background:none;border:0;text-align:left;padding:0;text-decoration:none;cursor:pointer}.portfolio img{display:block;width:100%;height:auto;max-height:75vh;object-fit:contain}.portfolio figure{margin:0;min-width:0}.portfolio-viewer{position:fixed;inset:0;z-index:200;background:white;overflow:auto;padding:24px}.portfolio-viewer img{max-height:80vh}.portfolio-viewer button{margin-bottom:16px}.portfolio :focus-visible{outline:2px solid #202020;outline-offset:4px}@media(max-width:600px){.portfolio-grid{grid-template-columns:minmax(0,1fr)}.portfolio nav{margin-bottom:24px}}
"""

_VIEWER = """'use client';
import {useEffect,useRef,useState} from 'react';
export default function Gallery({images,galleryLayout}) {
 const [selected,setSelected]=useState(null); const trigger=useRef(null); const closeButton=useRef(null);
 const close=()=>{setSelected(null);trigger.current?.focus();};
 useEffect(()=>{if(!selected)return;closeButton.current?.focus();const key=e=>{if(e.key==='Escape')close();if(e.key==='Tab'){e.preventDefault();closeButton.current?.focus();}};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key);},[selected]);
 return <><div className="portfolio-grid" data-gallery-layout={galleryLayout || 'grid'}>{images.map((image,i)=><figure key={image.material_id}><button onClick={e=>{trigger.current=e.currentTarget;setSelected(image);}}><img loading="lazy" src={'/images/'+image.material_id+'.jpg'} alt={image.alt}/><span>{image.title}</span></button></figure>)}</div>{selected&&<div className="portfolio-viewer" role="dialog" aria-modal="true" aria-label={selected.alt}><button ref={closeButton} onClick={close}>Close image</button><img src={'/images/'+selected.material_id+'.jpg'} alt={selected.alt}/><p>{selected.title}</p></div>}</>;
}
"""


def source_archive(document, revision, previews, package_versions):
    page = validate_document(document)
    if type(revision) is not int or not 1 <= revision <= 9007199254740991:
        raise ValueError('Invalid portfolio revision')
    refs = image_references(page)
    if set(previews) != set(refs):
        raise ValueError('Missing portfolio previews')
    total = 0
    for mid, data in previews.items():
        if type(data) is not bytes or not data or hashlib.sha256(data).hexdigest() != refs[mid]['preview_sha256']:
            raise ValueError('Portfolio preview integrity mismatch')
        total += len(data)
        if total > MAX_PREVIEW_BYTES:
            raise ValueError('Portfolio previews exceed 64 MiB')
    files = {
        'portfolio.json': canonical(page) + b'\n',
        'package.json': canonical({'name': 'photo-portfolio', 'version': '1.0.0', 'private': True, 'engines': {'node': '>=20.9.0'}, 'scripts': {'dev': 'next dev', 'build': 'next build --webpack', 'start': 'next start'}, 'dependencies': package_versions}) + b'\n',
        'app/globals.css': (PORTFOLIO_CSS + '.portfolio .portfolio-grid[data-gallery-layout="single"]{grid-template-columns:minmax(0,1fr)}\n.portfolio nav a{text-decoration:none;padding:4px}\n').encode(),
        'app/Gallery.jsx': _VIEWER.encode(),
        'app/layout.jsx': b"import './globals.css';\nimport page from '../portfolio.json';\nexport const metadata={title:page.name,description:page.headline};\nexport default function Layout({children}){return <html lang=\"en\"><body><main className=\"portfolio\"><nav aria-label=\"Portfolio\"><a href=\"/\">{page.name}</a>{page.categories.map(c=><a key={c.slug} href={'/'+c.slug}>{c.title}</a>)}<a href=\"/about\">About / Contact</a><a href=\"/resume\">Resume</a></nav>{children}</main></body></html>;}\n",
        'app/page.jsx': b"import page from '../portfolio.json';\nexport default function Home(){return <><h1>{page.headline}</h1><p>{page.introduction}</p><div className=\"portfolio-grid\">{page.categories.map(c=><a key={c.slug} href={'/'+c.slug}><img src={'/images/'+c.images[c.hero].material_id+'.jpg'} alt={c.images[c.hero].alt}/><h2>{c.title}</h2><p>{c.description}</p></a>)}</div></>;}\n",
        'app/[slug]/page.jsx': b"import page from '../../portfolio.json';\nimport Gallery from '../Gallery';\nexport const dynamicParams=false;\nexport function generateStaticParams(){return page.categories.map(c=>({slug:c.slug}));}\nexport default async function Category({params}){const {slug}=await params;const c=page.categories.find(c=>c.slug===slug);return <><h1>{c.title}</h1><p>{c.description}</p><Gallery images={c.images} galleryLayout={page.galleryLayout}/></>;}\n",
        'app/about/page.jsx': b"import page from '../../portfolio.json';\nexport default function About(){return <><h1>About {page.name}</h1><p>{page.about}</p><h2>Contact</h2><p>{page.contact.location}</p>{page.contact.email&&<a href={'mailto:'+page.contact.email}>{page.contact.email}</a>}</>;}\n",
        'app/resume/page.jsx': b"import page from '../../portfolio.json';\nexport default function Resume(){return <><h1>Resume</h1>{page.resume.map((s,i)=><section key={i}><h2>{s.heading}</h2>{s.entries.map((e,j)=><article key={j}><h3>{e.title}</h3><p>{e.subtitle}</p><p>{e.dates}</p><p>{e.description}</p></article>)}</section>)}</>;}\n",
        'next.config.mjs': b'export default {};\n',
        '.gitignore': b'node_modules/\n.next/\n.env*\n',
        'README.md': b'# Your photo portfolio\n\nRequires Node.js 20.9 or newer. Run `npm install`, `npm run build`, then `npm start`. Keep the generated package-lock.json and use npm ci for subsequent installs.\n\nEdit portfolio.json for literal text and galleries. Local optimized JPEG previews are included; originals remain independently downloadable from the project photo API. No Studio connection, secrets, or external image services are required at runtime. Saving or exporting does not publish. Deploy through your own hosting account when ready.\n\nsource-manifest.json binds the exact revision, canonical document, assets and every other file hash. It is not a build receipt.\n',
        'asset-manifest.json': canonical({mid: {'sha256': image['sha256'], 'preview_sha256': image['preview_sha256'], 'path': f'public/images/{mid}.jpg'} for mid, image in refs.items()}) + b'\n',
    }
    files.update({f'public/images/{mid}.jpg': data for mid, data in previews.items()})
    manifest = {'schema': 'leaf.portfolio-source.v1', 'revision': revision, 'document_sha256': hashlib.sha256(canonical(page)).hexdigest(), 'files': {path: {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)} for path, data in sorted(files.items())}}
    files['source-manifest.json'] = canonical(manifest) + b'\n'
    if sum(map(len, files.values())) > MAX_ARCHIVE_BYTES:
        raise ValueError('Portfolio source exceeds 80 MiB')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise ValueError('Portfolio archive exceeds 80 MiB')
    return result
