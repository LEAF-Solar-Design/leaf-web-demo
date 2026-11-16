import { authHeaders,getStoredOrgId,noteUnauthorized } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'
import { readActiveScope } from '../auth/activeScope.js'
import { copyChartState } from './privatePreviewState.js'
const UUID=/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/
const SHA=/^[0-9a-f]{64}$/
const bad=()=>{throw new Error('Chart state could not be verified. Your draft is still here.')}
const scope=()=>JSON.stringify([readActiveScope(),getStoredOrgId(),getStoredActorBindingId()])
export function chartDescriptor(result) {
  const c=result?.candidate,p=result?.preview
  if(result?.schema!=='leaf.browser-candidate-preview.v1'||!c||!p||!UUID.test(c.sourceRequestId)||!UUID.test(p.jobId)||!UUID.test(p.artifactId)||
    !Number.isSafeInteger(c.version)||c.version<1||c.version>2147483647||!SHA.test(c.receiptDigest)||!SHA.test(p.manifestSha256)||
    !/^[0-9a-f]{40}$/.test(c.sourceCommit)||!/^[0-9a-f]{40}$/.test(c.sourceTree)||p.sourceCommit!==c.sourceCommit||p.sourceTree!==c.sourceTree)bad()
  return Object.freeze({source_request_id:c.sourceRequestId,job_id:p.jobId,version:c.version,receipt_digest:c.receiptDigest,
    source_commit:c.sourceCommit,source_tree:c.sourceTree,artifact_id:p.artifactId,manifest_sha256:p.manifestSha256})
}
function projection(value,d) {
  if(!value||Object.keys(value).join(',')!=='chart')bad()
  const c=value.chart;if(c===null)return null
  if(!c||Object.keys(c).sort().join(',')!=='descriptor,digest,revision,state'||!Number.isSafeInteger(c.revision)||c.revision<1||c.revision>2147483647||!SHA.test(c.digest)||
    !c.descriptor||Object.keys(c.descriptor).sort().join(',')!==Object.keys(d).sort().join(',')||Object.keys(d).some(k=>c.descriptor[k]!==d[k]))bad()
  return {revision:c.revision,digest:c.digest,descriptor:d,state:copyChartState(c.state)}
}
async function readBody(response) {
  if(!response.headers.get('Content-Type')?.startsWith('application/json')||!response.body)bad()
  const reader=response.body.getReader(),chunks=[];let total=0
  try{for(;;){const part=await reader.read();if(part.done)break;total+=part.value.length;if(total>32768)bad();chunks.push(part.value)}}
  finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
  const bytes=new Uint8Array(total);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.length}
  return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes))
}
async function request({projectId,result,isCurrent,signal},save) {
  if(!UUID.test(projectId)||typeof isCurrent!=='function')bad()
  const d=chartDescriptor(result),held=scope(),abort=new AbortController()
  const current=()=>!abort.signal.aborted&&!signal?.aborted&&isCurrent()===true&&scope()===held
  if(!current())bad()
  const stop=()=>abort.abort();signal?.addEventListener('abort',stop,{once:true})
  const timer=setTimeout(stop,30000)
  try{
    const headers={...authHeaders()};if(!headers.Authorization||!readActiveScope())bad()
    const org=getStoredOrgId(),actor=getStoredActorBindingId()
    if(org)headers['X-Org-Id']=org;if(actor)headers['X-Actor-Binding-Id']=actor
    const {source_request_id,job_id,...transport}=d
    const base=`/api/projects/${projectId}/browser/source/requests/${source_request_id}/preview/${job_id}/chart-state`
    const path=save?base:`${base}?${new URLSearchParams(transport)}`
    if(save)headers['Content-Type']='application/json'
    const response=noteUnauthorized(await fetch(path,{method:save?'PUT':'GET',headers,body:save?JSON.stringify({descriptor:transport,...save}):undefined,
      signal:abort.signal,credentials:'same-origin',redirect:'error',cache:'no-store'}),path,headers.Authorization)
    if(!current())bad()
    if(!response.ok){const error=new Error(response.status===409?'The saved chart changed elsewhere. Your draft is still here. Reload before saving again.':'Chart state is unavailable. Your draft is still here.');error.status=response.status;throw error}
    const value=projection(await readBody(response),d);if(!current())bad()
    if(save&&(!value||value.revision!==save.expected_revision+1||JSON.stringify(value.state)!==JSON.stringify(save.state)))bad()
    return value
  }finally{clearTimeout(timer);signal?.removeEventListener('abort',stop)}
}
export const loadChartState=options=>request(options)
export function saveChartState(options,attempt) {
  if(!attempt||!Number.isSafeInteger(attempt.expected_revision)||attempt.expected_revision<0||attempt.expected_revision>=2147483647||!UUID.test(attempt.save_id))bad()
  const save={expected_revision:attempt.expected_revision,save_id:attempt.save_id,state:copyChartState(attempt.state)}
  return request(options,save)
}
