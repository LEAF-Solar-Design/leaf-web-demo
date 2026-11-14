/** Trusted fixed local fixture only. Never materializes tenant/evaluator source. */
import {mkdtemp,mkdir,writeFile,readFile,readdir,rm,symlink} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join,dirname} from 'node:path'
import {createRequire} from 'node:module'
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {chromium} from '@playwright/test'
const require=createRequire(import.meta.url), execute=promisify(execFile)
const next=require.resolve('next/dist/bin/next'), root=await mkdtemp(join(tmpdir(),'b03-frame-proof-')), project=join(root,'app')
let browser
try {
 await mkdir(join(project,'pages'),{recursive:true});await mkdir(join(project,'components'))
 await writeFile(join(project,'package.json'),JSON.stringify({private:true}))
 await writeFile(join(project,'next.config.mjs'),"export default {output:'export',experimental:{cpus:1}};\n")
 await writeFile(join(project,'pages/index.js'),`import {useState} from 'react';export default function Page(){const [Counter,setCounter]=useState(null);return <main><h1>B03 real private Next fixture</h1><button onClick={async()=>{const m=await import('../components/Counter');setCounter(()=>m.default)}}>Load counter</button>{Counter&&<Counter/>}</main>}`)
 await writeFile(join(project,'components/Counter.js'),`import {useState} from 'react';export default function Counter(){const [n,set]=useState(0);return <button onClick={()=>set(n+1)}>Count {n}</button>}`)
 await symlink(dirname(dirname(dirname(dirname(next)))),join(project,'node_modules'),'junction')
 await execute(process.execPath,[next,'build','--webpack'],{cwd:project,env:{NODE_ENV:'production',NEXT_TELEMETRY_DISABLED:'1'},timeout:120000,maxBuffer:1024*1024})
 const files=[]
 async function collect(dir,prefix=''){for(const entry of await readdir(dir,{withFileTypes:true})){const path=prefix+'/'+entry.name;if(entry.isDirectory())await collect(join(dir,entry.name),path);else if(entry.isFile())files.push({path,contentType:path.endsWith('.html')?'text/html':path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'application/octet-stream',bytes:[...await readFile(join(dir,entry.name))]});else throw Error('unexpected link')}}
 await collect(join(project,'out'))
 const source=await readFile(new URL('../src/site/opaqueStaticDocument.js',import.meta.url),'utf8')
 const libraryIndex=process.argv.indexOf('--library-root');const environment=libraryIndex<0?undefined:{LD_LIBRARY_PATH:process.argv[libraryIndex+1]};browser=await chromium.launch({headless:true,env:environment});const page=await browser.newPage()
 const errors=[];page.on('pageerror',error=>errors.push(error.message.slice(0,400)));page.on('console',message=>{if(message.type()==='error')errors.push(message.text().slice(0,400))});const network=[];await page.route('**/*',async route=>{const url=route.request().url();if(url==='https://parent.invalid/')await route.fulfill({contentType:'text/html',body:'<main>trusted parent</main>'});else{network.push(url);await route.abort()}})
 await page.goto('https://parent.invalid/')
 await page.evaluate(()=>localStorage.setItem('fixture-secret','must-not-cross-frame'))
 await page.evaluate(({source,files})=>{const build=new Function(source.replace(/^export /gm,'')+';return buildOpaqueStaticDocument')();const loaded={files:files.map(f=>({...f,bytes:new Uint8Array(f.bytes)}))};const iframe=document.createElement('iframe');iframe.title='B03 private fixture';iframe.setAttribute('sandbox','allow-scripts');iframe.srcdoc=build(loaded);document.body.append(iframe)},{source,files})
 const frame=page.frameLocator('iframe');await frame.getByRole('heading',{name:'B03 real private Next fixture'}).waitFor({timeout:15000})
 await frame.getByRole('button',{name:'Load counter',exact:true}).click();try{await frame.getByRole('button',{name:'Count 0',exact:true}).click({timeout:10000})}catch(error){console.log(JSON.stringify({errors,network,frameText:await frame.locator('body').innerText()}));throw error};await frame.getByRole('button',{name:'Count 1',exact:true}).waitFor({timeout:15000})
 const child=page.frames().find(f=>f!==page.mainFrame());if(!child)throw Error('missing child')
 const isolated=await child.evaluate(()=>{try{void parent.document.body;return false}catch{return true}})
 if(!isolated||network.length)throw Error('isolation/network proof failed: '+JSON.stringify({isolated,network}))
 console.log(JSON.stringify({proof:'real Next webpack dynamic chunk in opaque sandbox',next:require('next/package.json').version,files:files.length,counter:1,parentDocumentDenied:isolated,networkRequests:network,browserClosedInFinally:true}))
}finally{if(browser)await browser.close();await rm(root,{recursive:true,force:true})}
