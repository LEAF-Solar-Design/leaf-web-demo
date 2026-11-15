// Native-owned DXF byte repair inside the isolated worker boundary.
// Foreign written bytes and opaque generation parameters remain unchanged.
const SCHEMA = 'leaf.studio-native-layout.v1'
const NAMESPACE = 'LEAF_STUDIO_LAYOUT'
const encoder = new TextEncoder()
const fail = reason => { throw new Error('native_layout:' + reason) }
const numeric = new Set([10,20,30,38,39,40,41,42,62,70,90,1070,1071,1040,1041,210,220,230])
function binary(bytes) {
  let result = ''
  for (let i=0;i<bytes.length;i+=16384) result += String.fromCharCode(...bytes.subarray(i,i+16384))
  return result
}
function bytes(text) { return Uint8Array.from(text, c => c.charCodeAt(0)) }
function asciiJson(value) { return JSON.stringify(value).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0')) }
function tree(text) {
  JSON.parse(text)
  let i=0, count=0
  const space=()=>{while(/\s/.test(text[i]||'')&&i<text.length)i++}
  function string() { const start=i++; while(i<text.length){if(text[i]==='\\'){i+=2;continue}if(text[i++]==='"')return {start,end:i}}fail('json_string') }
  function value(depth=0) {
    if(depth>32||++count>1000000)fail('json_limit')
    space();const node={start:i}, c=text[i]
    if(c==='"')return string()
    if(c==='{'){
      node.props=new Map();i++;space()
      while(text[i]!=='}'){
        const key=string();const name=JSON.parse(text.slice(key.start,key.end));space();if(text[i++]!==':'||node.props.has(name))fail('json_key')
        node.props.set(name,value(depth+1));space();if(text[i]!==',')break;i++;space()
      }
      if(text[i++]!=='}')fail('json_object')
    }else if(c==='['){
      node.items=[];i++;space();while(text[i]!==']'){node.items.push(value(depth+1));space();if(text[i]!==',')break;i++;space()}
      if(text[i++]!==']')fail('json_array')
    }else{while(i<text.length&&!/[\s,}\]]/.test(text[i]))i++}
    node.end=i;return node
  }
  const root=value();space();if(i!==text.length)fail('json_tail');return root
}
function replace(text, edits) {
  let last=text.length
  for(const edit of [...edits].sort((a,b)=>b.start-a.start)){
    if(edit.end>last||edit.start>edit.end)fail('overlapping_edit')
    text=text.slice(0,edit.start)+edit.text+text.slice(edit.end);last=edit.start
  }
  return text
}
function drawing(input) {
  const text=binary(input),pairs=[];let at=0
  function line(){const start=at;while(at<text.length&&text[at]!=='\r'&&text[at]!=='\n')at++;const result=text.slice(start,at);if(text[at]==='\r')at++;if(text[at]==='\n')at++;return result}
  while(at<text.length){const start=at,raw=line();if(!/^\s*\d+\s*$/.test(raw))fail('group_code');if(at>=text.length)fail('group_value');const val=line();pairs.push({code:Number(raw),value:val,start,end:at})}
  const records=[];let section=null
  for(let i=0;i<pairs.length;){if(pairs[i].code!==0)fail('record');let end=i+1;while(end<pairs.length&&pairs[end].code!==0)end++
    const ps=pairs.slice(i,end),kind=ps[0].value
    if(kind==='SECTION')section=ps.find(p=>p.code===2)?.value
    const row={pairs:ps,kind,section,start:ps[0].start,end:ps.at(-1).end,values:code=>ps.filter(p=>p.code===code).map(p=>p.value)}
    records.push(row);if(kind==='ENDSEC')section=null;i=end
  }
  const handles=new Map()
  for(const row of records){if(row.section==='HEADER')continue;const hs=[...row.values(5),...row.values(105)];if(hs.length>1)fail('handle_count');if(hs.length){const h=hs[0].trim().toUpperCase();if(handles.has(h))fail('duplicate_handle');handles.set(h,row)}}
  return {text,records,handles}
}
const pack = pairs => pairs.map(p=>p.code+'\n'+p.value+'\n').join('')
async function sha(text) {
  const result=await globalThis.crypto.subtle.digest('SHA-256',encoder.encode(text))
  return [...new Uint8Array(result)].map(x=>x.toString(16).padStart(2,'0')).join('')
}
function number(value){if(typeof value==='string'&&!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim()))fail('number_syntax');const n=Number(value);if(!Number.isFinite(n)||Math.abs(n)>1e15)fail('coordinate');return n}
function g17(value){
  const n=number(value);if(n===0)return '0'
  const [mantissa,exponent]=n.toExponential(16).split('e');const e=Number(exponent)
  if(e < -4 || e >= 17)return mantissa.replace(/\.?0+$/,'')+'e'+(e<0?'-':'+')+String(Math.abs(e)).padStart(2,'0')
  const digits=mantissa.replace('.','').replace('-',''),sign=n<0?'-':'',point=e+1
  const fixed=point<=0?'0.'+'0'.repeat(-point)+digits:point>=digits.length?digits+'0'.repeat(point-digits.length):digits.slice(0,point)+'.'+digits.slice(point)
  return sign+(fixed.includes('.')?fixed.replace(/0+$/,'').replace(/\.$/,''):fixed)
}
function coordinates(row){const x=row.values(10).map(number),y=row.values(20).map(number);if(x.length!==4||y.length!==4)fail('frame_vertices');return x.map((v,i)=>[v,y[i]])}
function rigid(old,now){
  for(let i=0;i<4;i++)for(let j=0;j<i;j++){
    const a=Math.hypot(old[i][0]-old[j][0],old[i][1]-old[j][1]),b=Math.hypot(now[i][0]-now[j][0],now[i][1]-now[j][1])
    if(Math.abs(a-b)>Math.max(1e-7,1e-9*Math.max(a,b)))fail('frame_shape')
  }
  const area=p=>p.reduce((sum,v,i)=>sum+(v[0]-p[0][0])*(p[(i+1)%4][1]-p[0][1])-(v[1]-p[0][1])*(p[(i+1)%4][0]-p[0][0]),0)
  if(area(old)*area(now)<=0)fail('frame_winding')
}
export async function readNativeLayout(input){
  const text=binary(input);if(!text.includes(SCHEMA))return null
  const d=drawing(input),records=d.records.filter(r=>r.section==='OBJECTS'&&r.kind==='XRECORD'&&r.values(1)[0]===SCHEMA)
  if(records.length!==1)fail('namespace_count')
  const record=records[0],handle=record.values(5)[0]?.trim().toUpperCase()
  const owners=d.records.filter(r=>r.section==='OBJECTS'&&r.kind==='DICTIONARY'&&r.pairs.some((p,i)=>p.code===3&&p.value===NAMESPACE&&[350,360].includes(r.pairs[i+1]?.code)&&r.pairs[i+1].value.trim().toUpperCase()===handle))
  if(owners.length!==1)fail('namespace_owner')
  const raw=record.values(1).slice(1).join(''),ast=tree(raw),payload=JSON.parse(raw)
  if(await sha(payload.document)!==payload.document_sha256)fail('document_checksum')
  const params=ast.props.get('parameters');if(!params||await sha(raw.slice(params.start,params.end))!==payload.parameters_sha256)fail('parameters_checksum')
  const doc=JSON.parse(payload.document),docAst=tree(payload.document),frames=doc.state.frames
  if(!Array.isArray(frames)||frames.length>100000)fail('frames')
  const owned=new Map(),keys=[...frames.map(f=>f.handle),...(doc.state.piles||[]).map((_,i)=>'pile:'+i)]
  if(keys.length!==Object.keys(payload.handle_map).length)fail('membership')
  for(const key of keys){const h=payload.handle_map[key]?.toUpperCase(),row=d.handles.get(h);if(!row||row.section!=='ENTITIES'||owned.has(h))fail('ownership');owned.set(h,row)}
  for(const frame of frames){const actual=coordinates(owned.get(payload.handle_map[frame.handle].toUpperCase()));if(actual.some((p,i)=>p.some((x,j)=>x!==Number((frame.vertices[i].$tuple||frame.vertices[i])[j]))))fail('stale_parent')}
  return {drawing:d,record,raw,ast,payload,doc,docAst,owned}
}
export async function repairNativeLayout(parent,written){
  if(!parent)return written
  const d=drawing(written),edits=[],docEdits=[]
  const xrecord=d.handles.get(parent.record.values(5)[0].trim().toUpperCase())
  if(!xrecord||xrecord.values(1).slice(1).join('')!==parent.raw)fail('metadata_changed')
  const frameByHandle=new Map(parent.doc.state.frames.map((frame,i)=>[parent.payload.handle_map[frame.handle].toUpperCase(),{frame,index:i}]))
  for(const row of d.records){if(row.section==='ENTITIES'&&(row.values(8).some(v=>['LEAF-TRACKERS','LEAF-PILING'].includes(v.toUpperCase()))||row.values(1001).some(v=>['LEAFFRAMECELL','LEAFPILING'].includes(v.toUpperCase())))){if(!parent.owned.has(row.values(5)[0]?.trim().toUpperCase()))fail('managed_entity_added')}}
  for(const [handle,old] of parent.owned){
    const row=d.handles.get(handle);if(!row||row.section!=='ENTITIES')fail('owned_entity_missing')
    const codes=new Set(old.pairs.map(p=>p.code));const observed=row.pairs.filter(p=>!(p.code>=40&&p.code<=42&&!codes.has(p.code)&&number(p.value)===0))
    if(observed.length!==old.pairs.length)fail('owned_fields')
    const frame=frameByHandle.get(handle)
    for(let i=0;i<observed.length;i++){
      const a=old.pairs[i],b=observed[i];if(a.code!==b.code)fail('owned_codes')
      if(frame&&[10,20].includes(a.code))continue
      if(numeric.has(a.code)){if(number(a.value)!==number(b.value))fail('owned_attribute')}
      else if(a.value!==b.value)fail('owned_attribute')
    }
    let replacement=old.pairs
    if(frame){
      const vertices=coordinates(row);rigid(frame.frame.vertices.map(p=>p.$tuple||p),vertices)
      const path=parent.docAst.props.get('state').props.get('frames').items[frame.index].props.get('vertices')
      docEdits.push({...path,text:asciiJson(vertices.map((point,i)=>frame.frame.vertices[i].$tuple?{$tuple:point}:point))})
      let x=0,y=0;replacement=old.pairs.map(p=>({code:p.code,value:p.code===10?g17(vertices[x++][0]):p.code===20?g17(vertices[y++][1]):p.value}))
    }
    edits.push({start:row.start,end:row.end,text:pack(replacement)})
  }
  const document=replace(parent.payload.document,docEdits),documentNode=parent.ast.props.get('document'),digestNode=parent.ast.props.get('document_sha256')
  const payload=replace(parent.raw,[{...documentNode,text:asciiJson(document)},{...digestNode,text:asciiJson(await sha(document))}])
  const chunks=[];for(let i=0;i<payload.length;i+=255)chunks.push({code:1,value:payload.slice(i,i+255)})
  const pairs=[...parent.record.pairs.filter(p=>p.code!==1),{code:1,value:SCHEMA},...chunks]
  edits.push({start:xrecord.start,end:xrecord.end,text:pack(pairs)})
  const result=bytes(replace(d.text,edits));if(result.length>16*1024*1024)fail('document_limit');return result
}
