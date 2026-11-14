import {it,expect,vi} from "vitest";
import {createHash} from "node:crypto";
import {PrivateBuildLifecycle,createPrivateBuildServices} from "../src/projectBuild/privateBuildLifecycle.js";
import {GenericNextProducer,nextRequestDigest,staticManifestDigest} from "../src/projectBuild/genericNextBuild.js";
import * as runtimeSafety from "../src/runtimeSafety.js";
import {createHarness} from "../src/server.js";
import type {HarnessPorts} from "../src/ports/index.js";
import type {AddressInfo} from "node:net";
const id=(n:number)=>`00000000-0000-4000-8000-${String(n).padStart(12,"0")}`;
const a={tenantId:id(1),organizationId:id(2),projectId:id(3),repoKey:id(4)};
const r={jobId:id(5),sourceRequestId:id(6),version:1,receiptDigest:"c".repeat(64),sourceCommit:"a".repeat(40),sourceTree:"b".repeat(40)};
function fixture(){
 const body=Buffer.from("<h1>Fixture</h1>");
 const manifest={requestDigest:nextRequestDigest({...a,...r}),bundleSha256:"d".repeat(64),bindingId:id(7),toolchainSha256:"e".repeat(64),files:[{path:"/index.html",sizeBytes:body.length,sha256:createHash("sha256").update(body).digest("hex"),contentType:"text/html"}]};
 const preview={jobId:r.jobId,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree,bundleSha256:manifest.bundleSha256,bindingId:manifest.bindingId,toolchainSha256:manifest.toolchainSha256,artifactBindingId:id(8),artifactId:id(9),manifestSha256:staticManifestDigest(manifest),manifest};
 let resolve!:(p:typeof preview)=>void,reject!:(e:Error)=>void;
 const deferred=new Promise<typeof preview>((yes,no)=>{resolve=yes;reject=no});
 const producer={build:vi.fn((_authority:typeof a,_request:{jobId:string;sourceCommit:string;sourceTree:string})=>deferred),status:vi.fn(()=>({ready:true,cleanup:"confirmed" as const})),lastValid:vi.fn(()=>preview)};
 const loadCandidate=vi.fn(async()=>({schema:"leaf.browser-project-source.v1" as const,state:"published" as const,authority:a,...r}));
 const service=new PrivateBuildLifecycle({producer,loadCandidate});
 return {body,preview,producer,loadCandidate,service,resolve,reject};
}
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
it("status of an unknown job is read-only, including after process restart",async()=>{
 const f=fixture();expect((await f.service.observe(a,r)).status).toBe("unknown");expect(f.producer.build).not.toHaveBeenCalled();
});
it("simultaneous and repeated submissions keep one execution identity",async()=>{
 const f=fixture();const values=await Promise.all([f.service.submit(a,r),f.service.submit(a,r)]);
 expect(values.map(v=>v.status)).toEqual(["building","building"]);expect(f.producer.build).toHaveBeenCalledTimes(1);
 f.resolve(f.preview);await flush();expect((await f.service.observe(a,r)).result?.preview.artifactId).toBe(id(9));
 await f.service.submit(a,r);expect(f.producer.build).toHaveBeenCalledTimes(1);
});
it("lost acknowledgement is recovered by observation without resubmission",async()=>{
 const f=fixture();await f.service.submit(a,r);f.resolve(f.preview);await flush();
 expect((await f.service.observe(a,r)).status).toBe("ready");expect(f.producer.build).toHaveBeenCalledTimes(1);
});
it("failed and uncertain executions are never retried by status or repeated submit",async()=>{
 const f=fixture();await f.service.submit(a,r);f.reject(new Error("private provider details"));await flush();
 const value=await f.service.observe(a,r);expect(value).toEqual({schema:"leaf.private-static-build.v1",jobId:r.jobId,status:"failed"});
 expect((await f.service.submit(a,r)).status).toBe("failed");expect(f.producer.build).toHaveBeenCalledTimes(1);
});
it("refuses stale candidate and foreign project before execution",async()=>{
 const f=fixture();await expect(f.service.submit({...a,projectId:id(10)},r)).rejects.toHaveProperty("code","candidate_conflict");
 await expect(f.service.submit(a,{...r,sourceTree:"f".repeat(40)})).rejects.toHaveProperty("code","candidate_conflict");
 expect(f.producer.build).not.toHaveBeenCalled();
});
it("rejects a changed candidate witness on an existing job",async()=>{
 const f=fixture();await f.service.submit(a,r);
 f.loadCandidate.mockImplementation(async()=>({schema:"leaf.browser-project-source.v1",state:"published",authority:a,...r,version:2}));
 await expect(f.service.observe(a,{...r,version:2})).rejects.toHaveProperty("code","job_conflict");
});
it("no ready result escapes after cleanup or latest artifact changes",async()=>{
 const f=fixture();await f.service.submit(a,r);f.resolve(f.preview);await flush();
 f.producer.status.mockReturnValue({ready:false,cleanup:"confirmed"});await expect(f.service.observe(a,r)).rejects.toHaveProperty("code","preview_unavailable");
 f.producer.status.mockReturnValue({ready:true,cleanup:"confirmed"});f.producer.lastValid.mockReturnValue({...f.preview,jobId:id(11)});
 await expect(f.service.observe(a,r)).rejects.toHaveProperty("code","preview_unavailable");
});
it("rechecks candidate access across every observation",async()=>{
 const f=fixture();await f.service.submit(a,r);f.resolve(f.preview);await flush();
 f.loadCandidate.mockRejectedValue(new Error("revoked"));await expect(f.service.observe(a,r)).rejects.toThrow("revoked");
});
it("refuses ready output when cleanup changes during final candidate read",async()=>{
 const f=fixture();await f.service.submit(a,r);f.resolve(f.preview);await flush();let reads=0;
 f.loadCandidate.mockImplementation(async()=>{if(++reads===2)f.producer.status.mockReturnValue({ready:false,cleanup:"confirmed"});return {schema:"leaf.browser-project-source.v1",state:"published",authority:a,...r};});
 await expect(f.service.observe(a,r)).rejects.toHaveProperty("code","preview_unavailable");
});
it("snapshots mutable callers before awaiting candidate readback",async()=>{
 const f=fixture();const authority={...a},request={...r};const pending=f.service.submit(authority,request);
 authority.projectId=id(99);request.sourceTree="f".repeat(40);await pending;
 expect(f.producer.build.mock.calls[0]?.[0]).toEqual(a);expect(f.producer.build.mock.calls[0]?.[1]).toEqual({jobId:r.jobId,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree});
});
it("composition supplies the same real producer for reads and lifecycle",()=>{
 const producer=new GenericNextProducer({repository:{},isCurrent:async()=>true});const services=createPrivateBuildServices({producer,loadCandidate:async()=>null});
 expect(services.projectStaticPreviews).toBe(producer);expect(services.projectStaticBuilds).toBeInstanceOf(PrivateBuildLifecycle);
});
it("actual internal HTTP authenticates even with global auth disabled and status never submits",async()=>{
 const gate=vi.spyOn(runtimeSafety,"authoredExecutionEnabled").mockReturnValue(false);
 const f=fixture();const server=createHarness({} as HarnessPorts,{auth:{enabled:false,secret:"synthetic-only"},projectStaticBuilds:f.service}).listen();
 try{
  await new Promise<void>(done=>server.listening?done():server.once("listening",done));
  const url=`http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/project-static-preview/`;
  const payload={tenant_id:a.tenantId,organization_id:a.organizationId,project_id:a.projectId,repo_key:a.repoKey,job_id:r.jobId,source_request_id:r.sourceRequestId,version:r.version,receipt_digest:r.receiptDigest,source_commit:r.sourceCommit,source_tree:r.sourceTree};
  const call=(path:string,headers:Record<string,string>={},body=payload)=>fetch(url+path,{method:"POST",headers:{"content-type":"application/json",...headers},body:JSON.stringify(body)});
  expect((await call("build")).status).toBe(401);expect((await call("status",{"x-harness-secret":"wrong"})).status).toBe(401);
  const headers={"x-harness-secret":"synthetic-only","x-tenant-id":a.tenantId};
  expect((await call("build",{...headers,"x-tenant-id":id(99)})).status).toBe(409);
  expect((await call("status",headers)).status).toBe(200);expect(f.producer.build).not.toHaveBeenCalled();
  expect((await call("build",headers)).status).toBe(503);expect(f.producer.build).not.toHaveBeenCalled();gate.mockReturnValue(true);
  expect((await call("build",headers,{...payload,version:0})).status).toBe(409);expect(f.producer.build).not.toHaveBeenCalled();
  const duplicate=await fetch(url+"build",{method:"POST",headers:{"content-type":"application/json",...headers},body:JSON.stringify(payload).replace('"version":1','"version":1,"version":2')});
  expect(duplicate.status).toBe(409);expect(f.producer.build).not.toHaveBeenCalled();
  const response=await call("build",headers);expect(response.status).toBe(200);expect(response.headers.get("cache-control")).toBe("private, no-store");expect((await response.json()).status).toBe("building");
  f.resolve(f.preview);await flush();expect((await (await call("status",headers)).json()).status).toBe("ready");expect(f.producer.build).toHaveBeenCalledTimes(1);
 }finally{server.closeAllConnections();await new Promise<void>((yes,no)=>server.close(e=>e?no(e):yes()));gate.mockRestore();}
});
