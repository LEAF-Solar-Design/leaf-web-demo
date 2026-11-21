const assert=require('node:assert/strict')
const {rpc,copies,component,React,render,screen,fireEvent,waitFor,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Editor=component('X07CustomerProfileEditor')
async function main(){try{
  const a={customer_key:'a|b',project_key:'collision-a',drawing_key:'c'}
  const b={customer_key:'a',project_key:'collision-b',drawing_key:'b|c'}
  assert.equal(a.customer_key+'|'+a.drawing_key,b.customer_key+'|'+b.drawing_key)
  const pins=[];for(const context of [a,b])pins.push((await rpc(context,'confirm',context,{...copies[0],expected_version:0})).applied_profile)
  let decision='stay',release,oldSignal,saveCalls=0
  const props=(context,attempt=0)=>({customerKey:context.customer_key,drawingKey:context.drawing_key,leaveAttempt:attempt,
    leaveGuard:()=>Promise.resolve(decision),applicationRecord:pins[context===a?0:1],
    loadProfile:(customer,opts)=>rpc(context,'editor_load',context,customer,opts.signal),
    saveProfile:async(customer,roles,opts)=>{saveCalls++;oldSignal=opts.signal;return await new Promise(done=>{release=()=>done({...pins[0],version:999,roles})})}})
  const view=render(React.createElement(Editor,props(a)))
  await screen.findByText('Profile version: 1',{}, {timeout:15000});await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  fireEvent.change(screen.getByLabelText('Module block'),{target:{value:'pAnEl'}})
  view.rerender(React.createElement(Editor,props(b)))
  await screen.findByText('Profile edits kept. Staying on the current drawing.')
  assert.equal(screen.getByLabelText('Module block').value,'pAnEl');assert.ok(screen.getByText('Customer: '+a.customer_key))
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await waitFor(()=>assert.ok(release))
  decision='discard';view.rerender(React.createElement(Editor,props(b,1)))
  await screen.findByText('Customer: '+b.customer_key,{}, {timeout:15000})
  await waitFor(()=>assert.equal(screen.getByLabelText('Module block').value,'Panel'),{timeout:15000})
  assert.ok(oldSignal.aborted);release();await new Promise(done=>setImmediate(done))
  assert.ok(screen.getByText('Profile version: 1'));assert.equal(screen.queryByText('Profile version: 999'),null);assert.equal(saveCalls,1)
  for(const [index,context] of [a,b].entries())assert.deepEqual((await rpc(context,'load',context)).application_record,pins[index])
  console.log('PASS: delimiter-colliding scope pairs hold separate actual JSX state; explicit discard and delayed source receipt retirement preserve destination and pins')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
