/** Inert asset envelope for the authenticated Studio caller, never active HTML. */
import {createHash} from "node:crypto";
import {NextBuildError} from "./genericNextBuild.js";
import type {GenericNextProducer} from "./genericNextBuild.js";
import type {ProjectRepositoryAuthority} from "../ports/index.js";
export type PrivatePreviewReader = Pick<GenericNextProducer,"lastValid"|"status"> & {read(authority:ProjectRepositoryAuthority,jobId:string,path:string):Promise<{body:unknown;contentType:string}>};
export async function privatePreviewAsset(producer: PrivatePreviewReader,
 authority: ProjectRepositoryAuthority, witness: {jobId:string;sourceCommit:string;sourceTree:string;artifactId:string;manifestSha256:string;path:string}) {
 authority=Object.freeze({...authority});witness=Object.freeze({...witness});
 const matches=()=>{const p=producer.lastValid(authority);if(!p || p.jobId!==witness.jobId || p.sourceCommit!==witness.sourceCommit || p.sourceTree!==witness.sourceTree || p.artifactId!==witness.artifactId || p.manifestSha256!==witness.manifestSha256 || !producer.status(authority,witness.jobId).ready)throw new NextBuildError("preview_unavailable");return p;};
 const before=matches();const asset=await producer.read(authority,witness.jobId,witness.path);const after=matches();
 if(before!==after)throw new NextBuildError("preview_unavailable");
 const path=witness.path==="/"?"/index.html":witness.path;const file=before.manifest.files.find(f=>f.path===path);
 if(!file || !Buffer.isBuffer(asset.body) || asset.body.length!==file.sizeBytes || createHash("sha256").update(asset.body).digest("hex")!==file.sha256 || asset.contentType!==file.contentType)throw new NextBuildError("preview_response_invalid");
 return {schema:"leaf.private-static-asset.v1",job_id:before.jobId,source_commit:before.sourceCommit,source_tree:before.sourceTree,artifact_id:before.artifactId,manifest_sha256:before.manifestSha256,path,content_type:asset.contentType,size_bytes:asset.body.length,sha256:file.sha256,body_base64:asset.body.toString("base64")};
}
