// @vitest-environment jsdom
import {it,expect,vi,afterEach,beforeEach} from 'vitest'
import {chartDescriptor,loadChartState,saveChartState} from './privateChartStateClient.js'
const auth=vi.hoisted(()=>({scope:'account'}))
vi.mock('../auth/activeScope.js',()=>({readActiveScope:()=>auth.scope}))
vi.mock('../api.js',()=>({authHeaders:()=>({Authorization:'Bearer parent-only'}),getStoredOrgId:()=> 'org',noteUnauthorized:r=>r}))
vi.mock('../projects/api.js',()=>({getStoredActorBindingId:()=> 'actor'}))
const id=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const state={version:1,range:'all',scale:'Log',reference:'2023-01-01',averaging:56,timeAxis:'expanded2026',series:['a'],groups:[]}
const result={schema:'leaf.browser-candidate-preview.v1',candidate:{sourceRequestId:id(2),version:1,receiptDigest:'a'.repeat(64),sourceCommit:'b'.repeat(40),sourceTree:'c'.repeat(40)},preview:{jobId:id(3),artifactId:id(4),sourceCommit:'b'.repeat(40),sourceTree:'c'.repeat(40),manifestSha256:'d'.repeat(64)}}
const options={projectId:id(1),result,isCurrent:()=>true}
const chart=()=>({revision:3,digest:'e'.repeat(64),descriptor:chartDescriptor(result),state})
function response(value,status=200){const bytes=new TextEncoder().encode(JSON.stringify(value));let done=false;return{ok:status===200,status,headers:{get:()=> 'application/json'},body:{getReader:()=>({read:async()=>done?{done:true}:(done=true,{done:false,value:bytes}),cancel:async()=>{},releaseLock:()=>{}})}}}
beforeEach(()=>{auth.scope='account'})
afterEach(()=>vi.unstubAllGlobals())
it('reads only the scoped saved chart with parent authentication',async()=>{const fetch=vi.fn(async()=>response({chart:chart()}));vi.stubGlobal('fetch',fetch);expect((await loadChartState(options)).state).toEqual(state);const [path,init]=fetch.mock.calls[0];expect(path).toContain(`/api/projects/${id(1)}/browser/source/requests/${id(2)}/preview/${id(3)}/chart-state?`);expect(path).toContain('receipt_digest=');expect(init.method).toBe('GET');expect(init.headers.Authorization).toBe('Bearer parent-only');expect(init.body).toBeUndefined();expect(init.cache).toBe('no-store')})
it('returns null for an unsaved chart without inventing controls',async()=>{vi.stubGlobal('fetch',async()=>response({chart:null}));expect(await loadChartState(options)).toBeNull()})
it('writes a closed typed descriptor and checks the returned revision and state',async()=>{const fetch=vi.fn(async()=>response({chart:chart()}));vi.stubGlobal('fetch',fetch);const attempt={save_id:id(9),expected_revision:2,state};await saveChartState(options,attempt);const [path,init]=fetch.mock.calls[0],body=JSON.parse(init.body);expect(path.endsWith('/chart-state')).toBe(true);expect(init.method).toBe('PUT');expect(body).toEqual({descriptor:{version:1,receipt_digest:'a'.repeat(64),source_commit:'b'.repeat(40),source_tree:'c'.repeat(40),artifact_id:id(4),manifest_sha256:'d'.repeat(64)},...attempt});expect(init.body).not.toContain('parent-only')})
it('refuses saved state from another immutable preview',async()=>{const other=chart();other.descriptor={...other.descriptor,artifact_id:id(8)};vi.stubGlobal('fetch',async()=>response({chart:other}));await expect(loadChartState(options)).rejects.toThrow('could not be verified')})
it('drops a response after the active account changes',async()=>{let finish;vi.stubGlobal('fetch',()=>new Promise(r=>{finish=r}));const pending=loadChartState(options);auth.scope='other';finish(response({chart:chart()}));await expect(pending).rejects.toThrow('could not be verified')})
it('exposes a conflict without private server detail or a retry',async()=>{const fetch=vi.fn(async()=>response({secret:'do not expose'},409));vi.stubGlobal('fetch',fetch);await expect(saveChartState(options,{save_id:id(9),expected_revision:2,state})).rejects.toMatchObject({status:409,message:expect.stringContaining('Your draft is still here')});expect(fetch).toHaveBeenCalledTimes(1)})
it('rejects an oversized response and a false successful save projection',async()=>{vi.stubGlobal('fetch',async()=>response({chart:chart(),extra:'x'.repeat(33000)}));await expect(loadChartState(options)).rejects.toThrow();vi.stubGlobal('fetch',async()=>response({chart:{...chart(),revision:4}}));await expect(saveChartState(options,{save_id:id(9),expected_revision:2,state})).rejects.toThrow('could not be verified')})
