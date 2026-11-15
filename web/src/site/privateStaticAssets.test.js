import { it, expect } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import { loadPrivateStaticAssets } from './privateStaticAssets.js'
if (!globalThis.crypto?.subtle) Object.defineProperty(globalThis, 'crypto', { value: webcrypto })
const digest = b => createHash('sha256').update(b).digest('hex')
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
function fixture() {
 const bytes = Buffer.from('<main>inert</main>')
 const manifest = { requestDigest:'f'.repeat(64),bundleSha256:'a'.repeat(64),bindingId:id(7),toolchainSha256:'c'.repeat(64),files:[{path:'/index.html',sha256:digest(bytes),sizeBytes:bytes.length,contentType:'text/html'}] }
 const manifestSha256=digest(JSON.stringify([manifest.requestDigest,manifest.bundleSha256,manifest.bindingId,manifest.toolchainSha256,manifest.files.map(f=>[f.path,f.sha256,f.sizeBytes,f.contentType])]))
 const preview={jobId:id(5),artifactId:id(6),sourceCommit:'d'.repeat(40),sourceTree:'e'.repeat(40),manifestSha256,...manifest,manifest}
 const response={schema:'leaf.private-static-asset.v1',job_id:preview.jobId,artifact_id:preview.artifactId,source_commit:preview.sourceCommit,source_tree:preview.sourceTree,manifest_sha256:manifestSha256,path:'/index.html',sha256:digest(bytes),size_bytes:bytes.length,content_type:'text/html',body_base64:bytes.toString('base64')}
 return {preview,response,bytes}
}
it('returns verified bytes without creating an executable document',async()=>{const f=fixture();const loaded=await loadPrivateStaticAssets({preview:f.preview,requestAsset:async()=>f.response,isCurrent:()=>true});expect(Buffer.from(loaded.files[0].bytes)).toEqual(f.bytes);expect(loaded).not.toHaveProperty('srcDoc')})
it.each(['manifest','path','hash','foreign','base64','header'])('rejects %s drift',async kind=>{const f=fixture();if(kind==='manifest')f.preview.manifestSha256='0'.repeat(64);if(kind==='path')f.preview.manifest.files[0].path='/../index.html';if(kind==='hash')f.response.body_base64=Buffer.alloc(f.bytes.length).toString('base64');if(kind==='foreign')f.response.job_id=id(99);if(kind==='base64')f.response.body_base64+='=';if(kind==='header')f.response.content_type='text/html\r\nX:bad';await expect(loadPrivateStaticAssets({preview:f.preview,requestAsset:async()=>f.response,isCurrent:()=>true})).rejects.toThrow('private_preview_unavailable')})
it('refuses revocation after fetch',async()=>{const f=fixture();let live=true;await expect(loadPrivateStaticAssets({preview:f.preview,requestAsset:async()=>{live=false;return f.response},isCurrent:()=>live})).rejects.toThrow()})
it('snapshots caller metadata before async asset work',async()=>{const f=fixture();await expect(loadPrivateStaticAssets({preview:f.preview,requestAsset:async()=>{f.preview.sourceTree='0'.repeat(40);return f.response},isCurrent:()=>true})).resolves.toHaveProperty('files')})
it('refuses an aborted read before dispatch',async()=>{const f=fixture(),c=new AbortController();c.abort();let count=0;await expect(loadPrivateStaticAssets({preview:f.preview,requestAsset:async()=>{count++;return f.response},isCurrent:()=>true,signal:c.signal})).rejects.toThrow();expect(count).toBe(0)})
