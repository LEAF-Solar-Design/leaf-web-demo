import {it,expect} from "vitest";import {createHash} from "node:crypto";import {privatePreviewAsset} from "../src/projectBuild/privatePreviewAsset.js";
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,"0")}`;const a={tenantId:id(1),organizationId:id(2),projectId:id(3),repoKey:id(4)};const body=Buffer.from("<script>fixture</script>"),hash=createHash("sha256").update(body).digest("hex");
function fixture(){const w={jobId:id(5),sourceCommit:"a".repeat(40),sourceTree:"b".repeat(40),artifactId:id(6),manifestSha256:"c".repeat(64),path:"/"};const p={...w,bundleSha256:"d".repeat(64),bindingId:id(7),toolchainSha256:"e".repeat(64),artifactBindingId:id(8),manifest:{requestDigest:"f".repeat(64),bundleSha256:"d".repeat(64),bindingId:id(7),toolchainSha256:"e".repeat(64),files:[{path:"/index.html",sha256:hash,sizeBytes:body.length,contentType:"text/html"}]}};const producer={lastValid:()=>p,status:()=>({ready:true,cleanup:"confirmed" as const}),read:async()=>({body,contentType:"text/html"})};return {w,p,producer};}
it("returns only an inert exact-bound envelope",async()=>{const f=fixture(),r=await privatePreviewAsset(f.producer,a,f.w);expect(r.body_base64).toBe(body.toString("base64"));expect(r.content_type).toBe("text/html");expect(r.path).toBe("/index.html");});
it.each(["jobId","sourceCommit","sourceTree","artifactId","manifestSha256"] as const)("rejects foreign %s",async key=>{const f=fixture();f.w[key]="foreign";await expect(privatePreviewAsset(f.producer,a,f.w)).rejects.toHaveProperty("code");});
it("rejects producer read corruption",async()=>{const f=fixture();f.producer.read=async()=>({body:Buffer.from("bad"),contentType:"text/html"});await expect(privatePreviewAsset(f.producer,a,f.w)).rejects.toHaveProperty("code");});
it("rejects revocation during read",async()=>{const f=fixture();f.producer.read=async()=>{f.producer.status=()=>({ready:false,cleanup:"confirmed"});return {body,contentType:"text/html"}};await expect(privatePreviewAsset(f.producer,a,f.w)).rejects.toHaveProperty("code");});


import {createHarness} from "../src/server.js";
import type {HarnessPorts} from "../src/ports/index.js";
import type {AddressInfo} from "node:net";
it("internal asset HTTP requires secret and tenant match, returns inert JSON",async()=>{
 const f=fixture();const server=createHarness({} as HarnessPorts,{auth:{enabled:false,secret:"fixture-only-secret"},projectStaticPreviews:f.producer}).listen();
 try {await new Promise<void>(resolve=>server.listening?resolve():server.once("listening",resolve));const url=`http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/project-static-preview/asset`;
 const payload={tenant_id:a.tenantId,organization_id:a.organizationId,project_id:a.projectId,repo_key:a.repoKey,job_id:f.w.jobId,source_commit:f.w.sourceCommit,source_tree:f.w.sourceTree,artifact_id:f.w.artifactId,manifest_sha256:f.w.manifestSha256,path:"/"};
 const call=(headers:Record<string,string>)=>fetch(url,{method:"POST",headers:{"content-type":"application/json",...headers},body:JSON.stringify(payload)});
 expect((await call({})).status).toBe(401);expect((await call({"x-harness-secret":"wrong"})).status).toBe(401);
 expect((await call({"x-harness-secret":"fixture-only-secret","x-tenant-id":id(99)})).status).toBe(409);
 const response=await call({"x-harness-secret":"fixture-only-secret","x-tenant-id":a.tenantId});expect(response.status).toBe(200);expect(response.headers.get("content-type")).toContain("application/json");expect(response.headers.get("cache-control")).toBe("private, no-store");expect(await response.json()).toHaveProperty("body_base64",body.toString("base64"));
 }finally{server.closeAllConnections();await new Promise<void>((resolve,reject)=>server.close(e=>e?reject(e):resolve()));}
});
