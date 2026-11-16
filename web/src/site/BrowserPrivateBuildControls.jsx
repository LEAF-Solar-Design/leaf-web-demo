import { useEffect, useRef, useState } from 'react'
import { authHeaders, getStoredOrgId, isSourceUuid, noteUnauthorized, validateProjectSource } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { selectPrivateBuild } from './privateBuildSelection.js'

const witness = row => ({ source_request_id:row.source_request_id,version:row.version,receipt_digest:row.receipt_digest,source_commit:row.source_commit,source_tree:row.source_tree })
const scope = () => JSON.stringify([readActiveScope(),getStoredOrgId(),getStoredActorBindingId()])
async function readResult(response) {
  if (!response.ok || !response.headers.get('Content-Type')?.startsWith('application/json') || !response.body) throw new Error()
  const reader=response.body.getReader(),chunks=[];let total=0
  try { for (;;) { const part=await reader.read();if(part.done)break;total+=part.value.length;if(total>2*1024*1024)throw new Error();chunks.push(part.value) } }
  finally { await reader.cancel().catch(()=>{});reader.releaseLock() }
  const bytes=new Uint8Array(total);let offset=0
  for(const part of chunks){bytes.set(part,offset);offset+=part.length}
  return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes))
}
export default function BrowserPrivateBuildControls({ controller:c }) {
  const [revision,changed]=useState(0),liveKey=useRef(null)
  useEffect(()=>{
    const refresh=()=>changed(n=>n+1),unsubscribe=subscribeActiveScope(refresh)
    addEventListener('storage',refresh)
    return ()=>{unsubscribe();removeEventListener('storage',refresh)}
  },[])
  const row=c.candidate,account=readActiveScope(),heldScope=scope()
  const key=JSON.stringify([revision,heldScope,c.projectId,row&&witness(row)])
  liveKey.current=key
  let valid=false
  try { valid=c.sourceContext==='browser' && !!account && c.sourceLoaded && !c.unknown && row?.state==='published' && !!validateProjectSource(row,c.projectId,undefined,'browser') } catch { /* Only current verified source can build. */ }
  if(!valid)return null
  return <BuildSession key={key} controller={c} account={account} heldScope={heldScope} isCurrent={()=>liveKey.current===key&&scope()===heldScope} />
}
function BuildSession({controller:c,account,heldScope,isCurrent}) {
  const source=witness(c.candidate),sourceKey=JSON.stringify(source)
  const storageKey=`leaf.browser-private-build.v1:${JSON.stringify([heldScope,c.projectId,c.candidate.source_request_id])}`
  const owner=useRef({active:true,loaded:false,busy:false,abort:null,record:null}),[view,setView]=useState({loaded:false,busy:false,status:null,error:null,blocked:false})
  useEffect(()=>{
    const o=owner.current;o.active=true
    try{
      const raw=localStorage.getItem(storageKey)
      if(raw!==null){
        if(raw.length>4096)throw new Error()
        const record=JSON.parse(raw)
        if(!record||Object.keys(record).sort().join(',')!=='jobId,source'||!isSourceUuid(record.jobId)||JSON.stringify(record.source)!==sourceKey)throw new Error()
        o.record=record
      }
      o.loaded=true;setView({loaded:true,busy:false,status:o.record?'unknown':null,error:null,blocked:false})
    }catch{o.loaded=true;setView({loaded:true,busy:false,status:null,error:'The saved build identity is unavailable. Restore it before submitting another build.',blocked:true})}
    return ()=>{o.active=false;o.abort?.abort()}
  },[storageKey,sourceKey])
  const current=()=>owner.current.active&&isCurrent()
  const request=async submit=>{
    const o=owner.current
    if(!current()||!o.loaded||o.busy||view.blocked||c.busy||c.loading||!c.sourceLoaded||c.unknown)return
    if(submit){
      if(o.record)return
      const record={jobId:crypto.randomUUID(),source:JSON.parse(sourceKey)}
      try{localStorage.setItem(storageKey,JSON.stringify(record))}catch{setView(v=>({...v,blocked:true,error:'A build identity could not be saved. The build was not submitted.'}));return}
      o.record=record
    }
    if(!o.record)return
    o.busy=true;o.abort=new AbortController();setView({loaded:true,busy:true,status:'unknown',error:null,blocked:false})
    const timeout=setTimeout(()=>o.abort.abort(),25000)
    try{
      const headers={...authHeaders()};if(!headers.Authorization)throw new Error()
      const org=getStoredOrgId(),actor=getStoredActorBindingId()
      if(org)headers['X-Org-Id']=org
      if(actor)headers['X-Actor-Binding-Id']=actor
      if(submit)headers['Content-Type']='application/json'
      const base=`/api/projects/${encodeURIComponent(c.projectId)}/browser/source/requests/${encodeURIComponent(source.source_request_id)}/preview/${encodeURIComponent(o.record.jobId)}`
      const values={version:source.version,receipt_digest:source.receipt_digest,source_commit:source.source_commit,source_tree:source.source_tree}
      const path=submit?base:`${base}?${new URLSearchParams(values)}`
      const response=noteUnauthorized(await fetch(path,{method:submit?'POST':'GET',headers,body:submit?JSON.stringify(values):undefined,signal:o.abort.signal,credentials:'same-origin',cache:'no-store',redirect:'error'}),path,headers.Authorization)
      const value=await readResult(response)
      if(!current()||o.abort.signal.aborted)return
      if(value?.schema!=='leaf.private-static-build.v1'||value.jobId!==o.record.jobId||!['unknown','building','failed','ready'].includes(value.status))throw new Error()
      let result
      if(value.status==='ready'){
        result=value.result;const candidate=result?.candidate,p=result?.preview
        if(result?.schema!=='leaf.browser-candidate-preview.v1'||!candidate||!p||
          candidate.sourceRequestId!==source.source_request_id||candidate.version!==source.version||candidate.receiptDigest!==source.receipt_digest||
          candidate.sourceCommit!==source.source_commit||candidate.sourceTree!==source.source_tree||p.jobId!==o.record.jobId||p.sourceCommit!==source.source_commit||p.sourceTree!==source.source_tree)throw new Error()
      }
      setView({loaded:true,busy:false,status:value.status,result,error:null,blocked:false})
    }catch{if(current())setView({loaded:true,busy:false,status:'unknown',error:'Build outcome unknown. Check the saved build before any further submission.',blocked:false})}
    finally{clearTimeout(timeout);o.busy=false}
  }
  const open=()=>{
    if(!current()||view.status!=='ready'||!view.result)return
    if(selectPrivateBuild({accountScope:account,projectId:c.projectId,kind:'private-next',result:view.result,name:'Private app'}))c.close()
    else setView(v=>({...v,error:'The Browser workspace is unavailable. Reopen it before opening the app.'}))
  }
  return <section aria-label="Private app build">
    {!view.status&&<p>App build and private preview are still pending.</p>}
    {view.busy&&<p role="status">Checking private app build...</p>}
    {!view.busy&&view.status&&<p role="status">Private app build: {view.status}</p>}
    {view.error&&<p role="alert">{view.error}</p>}
    <button type="button" disabled={!view.loaded||view.busy||view.blocked||c.busy||c.loading||!!owner.current.record} onClick={()=>void request(true)}>Build private app</button>
    <button type="button" disabled={view.busy||view.blocked||!owner.current.record} onClick={()=>void request(false)}>Check app build</button>
    <button type="button" disabled={view.busy||view.status!=='ready'} onClick={open}>Open private app</button>
  </section>
}
