// @vitest-environment jsdom
import React from 'react'
import { render,screen,waitFor,cleanup,act,fireEvent } from '@testing-library/react'
import { it,expect,vi,beforeEach,afterEach } from 'vitest'
import { webcrypto } from 'node:crypto'
import BrowserPrivateBuildControls from './BrowserPrivateBuildControls.jsx'
import { BrowserAssetGround } from './BrowserAssetGround.jsx'
import { selectPrivateBuild,subscribePrivateBuildSelection } from './privateBuildSelection.js'
const auth=vi.hoisted(()=>({account:'fixture-account',listeners:new Set()}))
vi.mock('../auth/activeScope.js',()=>({readActiveScope:()=>auth.account,subscribeActiveScope:listener=>{auth.listeners.add(listener);return()=>auth.listeners.delete(listener)}}))
vi.mock('../api.js',()=>({authHeaders:()=>({Authorization:'Bearer synthetic-parent-only'}),getStoredOrgId:()=> 'fixture-org',noteUnauthorized:r=>r,isSourceUuid:s=>typeof s==='string'&&/^[0-9a-f-]{36}$/.test(s),validateProjectSource:row=>row}))
vi.mock('../projects/api.js',()=>({getStoredActorBindingId:()=> 'fixture-actor'}))
vi.mock('../auth.js',()=>({authConfigured:true,isSignedIn:()=>true,login:()=>{},subscribeBeforeLogout:()=>()=>{},subscribeTokenStored:()=>()=>{}}))
vi.mock('../lib/composerDraft.js',()=>({composerDraftAccountScope:()=>auth.account}))
vi.mock('./ProjectBoardGround.jsx',()=>({ProjectBoardGround:()=>null}))
vi.mock('./WorkspaceEnvironment.jsx',()=>({default:({children})=><>{children}</>}))
vi.mock('./ProjectPageEditor.jsx',()=>({default:()=>null,PagePreview:()=>null}))
vi.mock('./CanvasToolbar.jsx',()=>({default:()=>null}))
vi.mock('./PrivateStaticPreview.jsx',()=>({default:({result})=><div data-testid="mounted-private-result">{result.preview.jobId}</div>}))
const id=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const controller=()=>({sourceContext:'browser',projectId:id(1),sourceLoaded:true,unknown:false,busy:false,loading:false,close:vi.fn(),candidate:{state:'published',source_request_id:id(2),version:1,receipt_digest:'c'.repeat(64),source_commit:'a'.repeat(40),source_tree:'b'.repeat(40)}})
function response(value){const bytes=new TextEncoder().encode(JSON.stringify(value));let done=false;return{ok:true,headers:{get:()=> 'application/json'},body:{getReader:()=>({read:async()=>done?{done:true}:(done=true,{done:false,value:bytes}),cancel:async()=>{},releaseLock:()=>{}})}}}
const job=path=>path.split('?')[0].split('/').at(-1)
function observation(c,path,status='building'){
 const jobId=job(path),r=c.candidate
 return{schema:'leaf.private-static-build.v1',jobId,status,...(status==='ready'?{result:{schema:'leaf.browser-candidate-preview.v1',candidate:{sourceRequestId:r.source_request_id,version:r.version,receiptDigest:r.receipt_digest,sourceCommit:r.source_commit,sourceTree:r.source_tree},preview:{jobId,sourceCommit:r.source_commit,sourceTree:r.source_tree,artifactId:id(3)}}}:{})}
}
beforeEach(()=>{auth.account='fixture-account';localStorage.clear();Object.defineProperty(globalThis,'crypto',{value:webcrypto,configurable:true})})
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('saves identity before POST and fences double click, then status uses GET only',async()=>{
 const c=controller();let finish
 const fetch=vi.fn((path,opts)=>{const stored=JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.startsWith('leaf.browser-private-build.'))));expect(stored.jobId).toBe(job(path));expect(opts.headers.Authorization).toBe('Bearer synthetic-parent-only');return opts.method==='POST'?new Promise(resolve=>{finish=()=>resolve(response(observation(c,path)))}):Promise.resolve(response(observation(c,path)))})
 vi.stubGlobal('fetch',fetch);render(<BrowserPrivateBuildControls controller={c}/>);
 const button=await screen.findByRole('button',{name:'Build private app'});await waitFor(()=>expect(button.disabled).toBe(false))
 act(()=>{button.click();button.click()});await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));await act(async()=>finish())
 fireEvent.click(screen.getByRole('button',{name:'Check app build'}));await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));expect(fetch.mock.calls.map(([,o])=>o.method)).toEqual(['POST','GET']);expect(job(fetch.mock.calls[0][0])).toBe(job(fetch.mock.calls[1][0]));expect(button.disabled).toBe(true)
})
it('lost submission reply retains identity across reopen and never auto resubmits',async()=>{
 const c=controller(),fetch=vi.fn(async(path,opts)=>{if(opts.method==='POST')throw new Error('lost');return response(observation(c,path))});vi.stubGlobal('fetch',fetch)
 const first=render(<BrowserPrivateBuildControls controller={c}/>);fireEvent.click(await screen.findByRole('button',{name:'Build private app'}));await screen.findByRole('alert');first.unmount()
 render(<BrowserPrivateBuildControls controller={c}/>);await screen.findByText('Private app build: unknown');expect(fetch).toHaveBeenCalledTimes(1);expect(screen.getByRole('button',{name:'Build private app'}).disabled).toBe(true)
 fireEvent.click(screen.getByRole('button',{name:'Check app build'}));await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));expect(fetch.mock.calls[1][1].method).toBe('GET');expect(job(fetch.mock.calls[1][0])).toBe(job(fetch.mock.calls[0][0]))
})
it('storage failure refuses execution before any POST',async()=>{
 const c=controller(),fetch=vi.fn();vi.stubGlobal('fetch',fetch);vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('quota')})
 render(<BrowserPrivateBuildControls controller={c}/>);fireEvent.click(await screen.findByRole('button',{name:'Build private app'}));await screen.findByText('A build identity could not be saved. The build was not submitted.');expect(fetch).not.toHaveBeenCalled()
})
it('rejects foreign ready source and never publishes it',async()=>{
 const c=controller(),accept=vi.fn(()=>true),unsubscribe=subscribePrivateBuildSelection(accept)
 try{vi.stubGlobal('fetch',async path=>{const value=observation(c,path,'ready');value.result.candidate.sourceTree='f'.repeat(40);return response(value)});render(<BrowserPrivateBuildControls controller={c}/>);fireEvent.click(await screen.findByRole('button',{name:'Build private app'}));await screen.findByRole('alert');expect(screen.getByRole('button',{name:'Open private app'}).disabled).toBe(true);expect(accept).not.toHaveBeenCalled()}finally{unsubscribe()}
})
it('drops late replies after account changes',async()=>{
 const c=controller();let finish;vi.stubGlobal('fetch',path=>new Promise(resolve=>{finish=()=>resolve(response(observation(c,path,'ready')))}))
 render(<BrowserPrivateBuildControls controller={c}/>);fireEvent.click(await screen.findByRole('button',{name:'Build private app'}));await waitFor(()=>expect(finish).toBeTypeOf('function'))
 act(()=>{auth.account='another-account';for(const listener of auth.listeners)listener()});await act(async()=>finish());expect(screen.getByRole('button',{name:'Open private app'}).disabled).toBe(true)
})
it('opens the matching result in the actual scoped Browser ground',async()=>{
 const c=controller();vi.stubGlobal('fetch',async path=>response(observation(c,path,'ready')))
 render(<><BrowserAssetGround signedIn accountScope={auth.account} projectId={c.projectId} browserActive/><BrowserPrivateBuildControls controller={c}/></>)
 fireEvent.click(await screen.findByRole('button',{name:'Build private app'}));await screen.findByText('Private app build: ready');fireEvent.click(screen.getByRole('button',{name:'Open private app'}));expect(await screen.findByTestId('mounted-private-result')).toBeTruthy();expect(c.close).toHaveBeenCalledTimes(1)
 expect(selectPrivateBuild({accountScope:'foreign',projectId:c.projectId,kind:'private-next',result:observation(c,'/'+id(9),'ready').result})).toBe(false)
})
