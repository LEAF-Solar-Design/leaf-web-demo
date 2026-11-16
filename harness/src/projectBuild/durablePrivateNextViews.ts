/** Read-only consumption of host-normalized durable receipts. The NX3 host
 * owns raw receipt decoding, persistent mount admission and cleanup markers.
 * This module has no build, provider, latest-pointer or process-job dependency.
 */
import {createHash} from "node:crypto";
import type {ProjectRepositoryAuthority} from "../ports/index.js";
import {NextBuildError,nextRequestDigest,staticManifestDigest} from "./genericNextBuild.js";
import type {NextBuildScope,NextPreview,PrivateStaticArtifacts} from "./genericNextBuild.js";
import type {BrowserCandidateBuildRequest,BrowserCandidateWitness,PublishedBrowserCandidate} from "./browserCandidateBuild.js";
import type {PrivateBuildObservation} from "./privateBuildLifecycle.js";

/** Normalized view, NOT the raw leaf.static-next.v1 provider receipt schema.
 * load must enforce current scoped access and read the durable receipt plus
 * retention/cleanup marker. Missing, unreadable or expired data returns null.
 */
export interface DurableNextViewRecord {
  schema: "leaf.private-static-view.v1";
  authority: ProjectRepositoryAuthority;
  request: BrowserCandidateBuildRequest;
  executionId: string;
  providerBuildId: string;
  terminalState: "READY"|"FAILED"|"STOPPED";
  sandboxCleanupConfirmed: boolean;
  cleaned: boolean;
  preview: NextPreview;
}
const uuid=(v:unknown):v is string=>typeof v==="string"&&/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha=(v:unknown,n=64):v is string=>typeof v==="string"&&new RegExp(`^[0-9a-f]{${n}}$`).test(v);
const scopeKey=(a:ProjectRepositoryAuthority)=>JSON.stringify([a.tenantId,a.organizationId,a.projectId,a.repoKey]);
const candidate=(r:BrowserCandidateWitness):BrowserCandidateWitness=>Object.freeze({sourceRequestId:r.sourceRequestId,version:r.version,receiptDigest:r.receiptDigest,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree});
const witnessKey=(r:BrowserCandidateWitness)=>JSON.stringify([r.sourceRequestId,r.version,r.receiptDigest,r.sourceCommit,r.sourceTree]);
function fail():never{throw new NextBuildError("preview_unavailable")}
const pathOK=(v:unknown):v is string=>typeof v==="string"&&v.length<=2048&&/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(v)&&!v.split("/").some(p=>p==="."||p==="..");

export class DurablePrivateNextViews {
  constructor(private readonly deps:{
    catalog:{load(authority:ProjectRepositoryAuthority,request:BrowserCandidateBuildRequest):Promise<DurableNextViewRecord|null>};
    artifacts:Pick<PrivateStaticArtifacts,"read"|"bindingId">;
    loadCandidate(authority:ProjectRepositoryAuthority,requestId:string):Promise<PublishedBrowserCandidate|null>;
  }){}

  private snapshot(a:ProjectRepositoryAuthority,r:BrowserCandidateBuildRequest){
    if(!a||!r||![a.tenantId,a.organizationId,a.projectId,a.repoKey,r.jobId,r.sourceRequestId].every(uuid)||
      !Number.isSafeInteger(r.version)||r.version<1||!sha(r.receiptDigest)||!sha(r.sourceCommit,40)||!sha(r.sourceTree,40))fail();
    return {a:Object.freeze({tenantId:a.tenantId,organizationId:a.organizationId,projectId:a.projectId,repoKey:a.repoKey}),r:Object.freeze({jobId:r.jobId,...candidate(r)})};
  }
  private async current(a:ProjectRepositoryAuthority,r:BrowserCandidateBuildRequest){
    const row=await this.deps.loadCandidate(a,r.sourceRequestId);
    if(!row||row.schema!=="leaf.browser-project-source.v1"||row.state!=="published"||!row.authority||scopeKey(row.authority)!==scopeKey(a)||witnessKey(row)!==witnessKey(r))fail();
  }
  private validate(a:ProjectRepositoryAuthority,r:BrowserCandidateBuildRequest,value:DurableNextViewRecord):NextPreview{
    try{
      // Snapshot mutable host/caller metadata before any further await.
      const raw=JSON.stringify(value);if(Buffer.byteLength(raw)>2*1024*1024)fail();
      const record=JSON.parse(raw) as DurableNextViewRecord;
      if(record.schema!=="leaf.private-static-view.v1"||!record.authority||scopeKey(record.authority)!==scopeKey(a)||
        !record.request||record.request.jobId!==r.jobId||witnessKey(record.request)!==witnessKey(r)||
        record.terminalState!=="READY"||record.sandboxCleanupConfirmed!==true||record.cleaned!==false||
        typeof record.providerBuildId!=="string"||!record.providerBuildId||record.providerBuildId.length>512)fail();
      const s:NextBuildScope={...a,jobId:r.jobId,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree};
      if(record.executionId!==nextRequestDigest(s))fail();
      const p=record.preview;
      if(!p||p.jobId!==r.jobId||p.sourceCommit!==r.sourceCommit||p.sourceTree!==r.sourceTree||
        !sha(p.bundleSha256)||!uuid(p.bindingId)||!sha(p.toolchainSha256)||!uuid(p.artifactBindingId)||p.artifactBindingId!==this.deps.artifacts.bindingId||!uuid(p.artifactId)||!sha(p.manifestSha256))fail();
      const m=p.manifest;
      if(!m||m.requestDigest!==record.executionId||m.bundleSha256!==p.bundleSha256||m.bindingId!==p.bindingId||m.toolchainSha256!==p.toolchainSha256||!Array.isArray(m.files)||m.files.length<1||m.files.length>4096)fail();
      const paths=new Set<string>();let total=0;
      for(const f of m.files){
        if(!f||!pathOK(f.path)||paths.has(f.path)||!sha(f.sha256)||!Number.isSafeInteger(f.sizeBytes)||f.sizeBytes<0||f.sizeBytes>2*1024*1024||
          typeof f.contentType!=="string"||f.contentType.length>256||! /^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(f.contentType))fail();
        paths.add(f.path);total+=f.sizeBytes;
      }
      if(!paths.has("/index.html")||total>32*1024*1024||staticManifestDigest(m)!==p.manifestSha256)fail();
      return Object.freeze({jobId:r.jobId,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree,bundleSha256:p.bundleSha256,
        bindingId:p.bindingId,toolchainSha256:p.toolchainSha256,artifactBindingId:p.artifactBindingId,artifactId:p.artifactId,manifestSha256:p.manifestSha256,
        manifest:Object.freeze({requestDigest:m.requestDigest,bundleSha256:m.bundleSha256,bindingId:m.bindingId,toolchainSha256:m.toolchainSha256,
          files:Object.freeze(m.files.map(f=>Object.freeze({path:f.path,sha256:f.sha256,sizeBytes:f.sizeBytes,contentType:f.contentType})))})});
    }catch{ return fail(); }
  }
  private async ready(a:ProjectRepositoryAuthority,r:BrowserCandidateBuildRequest):Promise<NextPreview|null>{
    await this.current(a,r);
    const record=await this.deps.catalog.load(a,r);
    const p=record===null?null:this.validate(a,r,record);
    await this.current(a,r);
    if(p===null)return null;
    // Re-read durable lifecycle state after the awaited authority check.
    const settled=await this.deps.catalog.load(a,r);
    if(settled===null)fail();
    const after=this.validate(a,r,settled);
    await this.current(a,r);
    if(after.manifestSha256!==p.manifestSha256||after.artifactId!==p.artifactId||after.bundleSha256!==p.bundleSha256||after.artifactBindingId!==p.artifactBindingId)fail();
    return after;
  }
  async observe(authority:ProjectRepositoryAuthority,request:BrowserCandidateBuildRequest):Promise<PrivateBuildObservation>{
    const {a,r}=this.snapshot(authority,request),preview=await this.ready(a,r);
    return Object.freeze({schema:"leaf.private-static-build.v1",jobId:r.jobId,status:preview?"ready":"unknown",
      ...(preview?{result:Object.freeze({schema:"leaf.browser-candidate-preview.v1" as const,candidate:candidate(r),preview})}:{})});
  }
  async asset(authority:ProjectRepositoryAuthority,request:BrowserCandidateBuildRequest,witness:{artifactId:string;manifestSha256:string;path:string}){
    const {a,r}=this.snapshot(authority,request);witness=Object.freeze({...witness,path:witness.path==="/"?"/index.html":witness.path});
    if(!pathOK(witness.path))fail();
    const p=await this.ready(a,r);
    if(!p||p.artifactId!==witness.artifactId||p.manifestSha256!==witness.manifestSha256)fail();
    const file=p.manifest.files.find(f=>f.path===witness.path);if(!file)fail();
    const s:NextBuildScope={...a,jobId:r.jobId,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree};
    const value=await this.deps.artifacts.read(s,p.artifactId,witness.path,2*1024*1024);
    if(!Buffer.isBuffer(value.body)||value.body.length!==file.sizeBytes||value.contentType!==file.contentType||createHash("sha256").update(value.body).digest("hex")!==file.sha256)fail();
    const body=Buffer.from(value.body),after=await this.ready(a,r);
    if(!after||after.artifactId!==p.artifactId||after.manifestSha256!==p.manifestSha256||after.artifactBindingId!==p.artifactBindingId)fail();
    return {schema:"leaf.private-static-asset.v1",job_id:r.jobId,source_commit:r.sourceCommit,source_tree:r.sourceTree,
      artifact_id:p.artifactId,manifest_sha256:p.manifestSha256,path:witness.path,content_type:file.contentType,size_bytes:body.length,sha256:file.sha256,body_base64:body.toString("base64")};
  }
}
