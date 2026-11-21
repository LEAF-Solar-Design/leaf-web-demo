import {useEffect,useRef,useState} from 'react'
import PrivateStaticPreview from './PrivateStaticPreview.jsx'
import {copyChartState} from './privatePreviewState.js'
import {loadChartState,saveChartState} from './privateChartStateClient.js'
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b)
export default function SavedPrivateStaticPreview({projectId,result,isCurrent,initialState,onStateChange}) {
  const [view,setView]=useState(null),live=useRef(null),held=useRef(null),initial=useRef(initialState),notify=useRef(onStateChange)
  held.current={projectId,result};initial.current=initialState;notify.current=onStateChange
  const current=o=>live.current===o&&o.active&&held.current.projectId===o.projectId&&held.current.result===o.result&&isCurrent()
  const show=o=>{if(current(o))setView({...o})}
  const load=async(o,replace=false)=>{
    if(!current(o)||o.busy)return
    o.busy=true;o.error=null;show(o)
    try{
      const saved=await loadChartState({projectId,result,isCurrent:()=>current(o),signal:o.abort.signal})
      if(!current(o))return
      o.revision=saved?.revision||0;o.saved=saved?.state||null
      if(replace&&saved){o.draft=saved.state;notify.current?.(copyChartState(saved.state))}
      else if(!o.loaded&&o.draft===null&&saved)o.draft=saved.state
      o.loaded=true;o.pending=null;o.conflict=false;o.dirty=o.draft!==null&&!same(o.draft,o.saved)
    }catch{if(current(o))o.error='Saved chart state is unavailable. Your draft is still here.'}
    finally{if(current(o)){o.busy=false;show(o)}}
  }
  useEffect(()=>{
    let draft=null;try{if(initial.current!=null)draft=copyChartState(initial.current)}catch{/* Invalid initial state never reaches the frame. */}
    const o={projectId,result,active:true,abort:new AbortController(),busy:false,loaded:false,revision:0,saved:null,draft,dirty:!!draft,pending:null,error:null,conflict:false}
    live.current=o;show(o);void load(o)
    return()=>{o.active=false;o.abort.abort();if(live.current===o)live.current=null}
  },[projectId,result,isCurrent])
  const changed=state=>{
    const o=live.current;if(!o||!current(o))return
    try{o.draft=copyChartState(state);o.dirty=!same(o.draft,o.saved);notify.current?.(copyChartState(o.draft));show(o)}catch{/* Retain the last valid draft. */}
  }
  const save=async()=>{
    const o=live.current;if(!o||!current(o)||o.busy||!o.loaded||o.conflict||!o.draft||(!o.dirty&&!o.pending))return
    o.pending ||= {save_id:crypto.randomUUID(),expected_revision:o.revision,state:copyChartState(o.draft)}
    const attempt=o.pending;o.busy=true;o.error=null;show(o)
    try{
      const saved=await saveChartState({projectId,result,isCurrent:()=>current(o),signal:o.abort.signal},attempt)
      if(!current(o))return
      o.revision=saved.revision;o.saved=saved.state;o.pending=null;o.dirty=!same(o.draft,o.saved)
    }catch(error){if(current(o)){if(error.status===409){o.conflict=true;o.pending=null}o.error=error.status===409?error.message:'Save outcome unknown. Your draft is still here. Retry the same save.'}}
    finally{if(current(o)){o.busy=false;show(o)}}
  }
  const o=live.current,shown=o&&current(o)&&view?.abort===o.abort?view:null
  if(!shown)return <p role="status">Loading saved chart...</p>
  return <section aria-label="Saved chart preview" style={{position:'absolute',inset:0,display:'flex',flexDirection:'column'}}>
    <div style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center',padding:4,flexShrink:0}}>
      <button type="button" disabled={shown.busy||!shown.loaded||shown.conflict||(!shown.dirty&&!shown.pending)} onClick={()=>void save()}>{shown.pending?'Retry chart save':'Save chart'}</button>
      <button type="button" disabled={shown.busy} onClick={()=>void load(o,true)}>Reload saved chart</button>
      <span role="status">{shown.busy?'Checking chart state...':shown.dirty?'Unsaved chart changes':shown.revision?`Saved chart revision ${shown.revision}`:'No saved chart state'}</span>
      {shown.error&&<p role="alert" style={{margin:0,width:'100%'}}>{shown.error}</p>}
      {shown.dirty&&<span>Reload replaces this draft with the saved chart.</span>}
    </div>
    <div style={{position:'relative',flex:1,minHeight:0}}>{shown.loaded&&<PrivateStaticPreview projectId={projectId} result={result} isCurrent={isCurrent} initialState={shown.draft} onStateChange={changed}/>}</div>
  </section>
}
