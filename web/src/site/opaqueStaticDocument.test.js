// @vitest-environment jsdom
import { it, expect } from 'vitest'
import { buildOpaqueStaticDocument, PRIVATE_FRAME_SANDBOX } from './opaqueStaticDocument.js'
const bytes = text => new TextEncoder().encode(text)
const fixture = html => ({files:[{path:'/index.html',contentType:'text/html',bytes:bytes(html)}]})
it('requires an opaque sandbox and places denying CSP before scripts',()=>{const doc=buildOpaqueStaticDocument(fixture('<main>ok</main>'));expect(PRIVATE_FRAME_SANDBOX).toBe('allow-scripts');expect(doc.indexOf('Content-Security-Policy')).toBeLessThan(doc.indexOf('<script'));expect(doc).toContain("connect-src 'none'")})
it('rewrites static chunk references to verified data URLs',()=>{const f=fixture('<script src="/_next/a.js"></script>');f.files.push({path:'/_next/a.js',contentType:'text/javascript',bytes:bytes('window.fixture=1')});const doc=buildOpaqueStaticDocument(f);expect(doc).toContain('src="data:text/javascript;base64,');expect(doc).not.toContain('src="/_next/a.js"')})
it('refuses remote and missing scripts rather than fetch a fallback',()=>{for(const url of ['https://example.invalid/a.js','/missing.js'])expect(()=>buildOpaqueStaticDocument(fixture(`<script src="${url}"></script>`))).toThrow()})
it('removes nested documents and author-provided base/refresh policies',()=>{const doc=buildOpaqueStaticDocument(fixture('<base href="https://remote.invalid"><meta http-equiv="refresh" content="0;url=https://remote.invalid"><iframe src="https://remote.invalid"></iframe>'));expect(doc).not.toContain('remote.invalid')})
