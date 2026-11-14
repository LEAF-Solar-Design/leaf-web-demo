import {it,expect,vi} from "vitest";
import {createHash} from "node:crypto";
import {mkdtemp,readFile,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {DurablePrivateNextViews} from "../src/projectBuild/durablePrivateNextViews.js";
import type {DurableNextViewRecord} from "../src/projectBuild/durablePrivateNextViews.js";
import {DiskPrivateStaticArtifacts} from "../src/projectBuild/privateStaticArtifactStore.js";
import {nextRequestDigest,staticManifestDigest} from "../src/projectBuild/genericNextBuild.js";
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,"0")}`;
const a={tenantId:id(1),organizationId:id(2),projectId:id(3),repoKey:id(4)};
const r={jobId:id(5),sourceRequestId:id(6),version:1,receiptDigest:"c".repeat(64),sourceCommit:"a".repeat(40),sourceTree:"b".repeat(40)};
const s={...a,jobId:r.jobId,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree};
function fixture(){
 const body=Buffer.from("<h1>disk fixture</h1>"),executionId=nextRequestDigest(s);
 const manifest={requestDigest:executionId,bundleSha256:"d".repeat(64),bindingId:id(7),toolchainSha256:"e".repeat(64),files:[{path:"/index.html",sha256:createHash("sha256").update(body).digest("hex"),sizeBytes:body.length,contentType:"text/html"}]};
 const record:DurableNextViewRecord={schema:"leaf.private-static-view.v1",authority:a,request:r,executionId,providerBuildId:"synthetic-provider-observation",terminalState:"READY",sandboxCleanupConfirmed:true,cleaned:false,
  preview:{...s,bundleSha256:manifest.bundleSha256,bindingId:manifest.bindingId,toolchainSha256:manifest.toolchainSha256,artifactBindingId:id(8),artifactId:id(9),manifestSha256:staticManifestDigest(manifest),manifest}};
 const catalog={load:vi.fn(async()=>record as DurableNextViewRecord|null)};
 const artifacts={bindingId:id(8),read:vi.fn(async()=>({body,contentType:"text/html"}))};
 const loadCandidate=vi.fn(async()=>({schema:"leaf.browser-project-source.v1" as const,state:"published" as const,authority:a,...r}));
 const views=new DurablePrivateNextViews({catalog,artifacts,loadCandidate});
 const witness={artifactId:record.preview.artifactId,manifestSha256:record.preview.manifestSha256,path:"/"};
 return {body,manifest,record,catalog,artifacts,loadCandidate,views,witness};
}
it("missing durable receipt is unknown and never reads bytes or submits work",async()=>{
 const f=fixture();f.catalog.load.mockResolvedValue(null);
 expect(await f.views.observe(a,r)).toEqual({schema:"leaf.private-static-build.v1",jobId:r.jobId,status:"unknown"});
 await expect(f.views.asset(a,r,f.witness)).rejects.toHaveProperty("code","preview_unavailable");expect(f.artifacts.read).not.toHaveBeenCalled();
});
it("ready observation and inert asset depend on the full stored candidate and scope",async()=>{
 const f=fixture();expect((await f.views.observe(a,r)).status).toBe("ready");
 expect(Object.keys((await f.views.observe(a,r)).result!.preview)).not.toContain("repoKey");
 const result=await f.views.asset(a,r,f.witness);expect(result.body_base64).toBe(f.body.toString("base64"));expect(result.path).toBe("/index.html");
 await expect(f.views.observe({...a,projectId:id(99)},r)).rejects.toHaveProperty("code");
 await expect(f.views.observe(a,{...r,version:2})).rejects.toHaveProperty("code");
});
it.each(["cleanup","cleaned","failed","execution","scope","manifest","candidate","store"])("refuses receipt %s corruption",async kind=>{
 const f=fixture();
 if(kind==="cleanup")f.record.sandboxCleanupConfirmed=false;
 if(kind==="cleaned")f.record.cleaned=true;
 if(kind==="failed")f.record.terminalState="FAILED";
 if(kind==="execution")f.record.executionId="f".repeat(64);
 if(kind==="scope")f.record.authority={...a,repoKey:id(99)};
 if(kind==="manifest")f.record.preview={...f.record.preview,manifestSha256:"f".repeat(64)};
 if(kind==="store")f.record.preview={...f.record.preview,artifactBindingId:id(99)};
 if(kind==="candidate")f.record.request={...r,receiptDigest:"f".repeat(64)};
 await expect(f.views.observe(a,r)).rejects.toHaveProperty("code");expect(f.artifacts.read).not.toHaveBeenCalled();
});
it("rechecks durable cleanup and candidate authority after byte read",async()=>{
 const f=fixture();f.artifacts.read.mockImplementation(async()=>{f.record.cleaned=true;return {body:f.body,contentType:"text/html"}});
 await expect(f.views.asset(a,r,f.witness)).rejects.toHaveProperty("code");
 const g=fixture();g.artifacts.read.mockImplementation(async()=>{g.loadCandidate.mockRejectedValue(new Error("revoked"));return {body:g.body,contentType:"text/html"}});
 await expect(g.views.asset(a,r,g.witness)).rejects.toThrow("revoked");
});
it("rejects corrupt bytes even if durable metadata remains ready",async()=>{
 const f=fixture();f.artifacts.read.mockResolvedValue({body:Buffer.from("wrong"),contentType:"text/html"});
 await expect(f.views.asset(a,r,f.witness)).rejects.toHaveProperty("code");
});
it("rechecks authority after a later catalog await",async()=>{
 const f=fixture();let calls=0;
 f.catalog.load.mockImplementation(async()=>{if(++calls===2)f.loadCandidate.mockRejectedValue(new Error("revoked"));return f.record});
 await expect(f.views.observe(a,r)).rejects.toThrow("revoked");
});
it("fresh reader reopens real disk custody without producer job maps",async()=>{
 const f=fixture(),root=await mkdtemp(join(tmpdir(),"b03-durable-view-"));
 try{
  const artifacts=new DiskPrivateStaticArtifacts({root:join(root,"assets"),bindingId:id(8),isCurrent:async()=>true});
  const stored=await artifacts.put(s,f.manifest,[{path:"/index.html",body:f.body,contentType:"text/html"}]);
  f.record.preview={...f.record.preview,...stored};const receipt=join(root,"normalized-host-view.json");await writeFile(receipt,JSON.stringify(f.record));
  // This JSON is a synthetic host-normalized receipt fixture, not a provider attestation.
  const reopen=()=>new DurablePrivateNextViews({catalog:{load:async()=>JSON.parse(await readFile(receipt,"utf8")) as DurableNextViewRecord},
   artifacts:new DiskPrivateStaticArtifacts({root:join(root,"assets"),bindingId:id(8),isCurrent:async()=>true}),loadCandidate:f.loadCandidate});
  expect((await reopen().observe(a,r)).status).toBe("ready");
  const result=await reopen().asset(a,r,{...f.witness,artifactId:stored.artifactId});expect(result.body_base64).toBe(f.body.toString("base64"));
  f.record.cleaned=true;await writeFile(receipt,JSON.stringify(f.record));await expect(reopen().observe(a,r)).rejects.toHaveProperty("code");
 }finally{await rm(root,{recursive:true,force:true})}
});
