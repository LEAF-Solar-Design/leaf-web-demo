const assert=require('node:assert/strict')
const {rpc,ctx,copies,component,React,render,screen,fireEvent,waitFor,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Editor=component('X07CustomerProfileEditor')
async function main(){try{
  const a=ctx('guard-a'),b={customer_key:'synthetic-b',project_key:'guard-b',drawing_key:'b:v1'}
  const first=await rpc(a,'confirm',a,{...copies[0],expected_version:0});const second=await rpc(b,'confirm',b,{...copies[0],expected_version:0})
  let decision='stay',guardCalls=[],release,oldSignal,saves=0
  const props=(context,attempt=0)=>({customerKey:context.customer_key,drawingKey:context.drawing_key,leaveAttempt:attempt,
    leaveGuard:request=>{guardCalls.push(request);return Promise.resolve(decision)},
    applicationRecord:context.customer_key===a.customer_key?first.applied_profile:second.applied_profile,
    loadProfile:(customer,opts)=>rpc(context,'editor_load',context,customer,opts.signal),
    saveProfile:async(customer,roles,opts)=>{if(context.customer_key===a.customer_key){oldSignal=opts.signal;await new Promise(done=>{release=done});if(opts.signal.aborted)return}
      saves++;return rpc(context,'editor_save',context,{customer,roles,expected_version:opts.expected_version},opts.signal)}})
  const view=render(React.createElement(Editor,props(a)))
  await screen.findByText('Profile version: 1',{}, {timeout:15000});await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  fireEvent.change(screen.getByLabelText('Module block'),{target:{value:'pAnEl'}})
  view.rerender(React.createElement(Editor,props(b)))
  await screen.findByText('Profile edits kept. Staying on the current drawing.')
  assert.equal(screen.getByLabelText('Module block').value,'pAnEl');assert.ok(screen.getByText('Customer: '+a.customer_key))
  assert.equal(guardCalls[0].from.customerKey,a.customer_key);assert.equal(guardCalls[0].to.customerKey,b.customer_key)
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await waitFor(()=>assert.ok(release))
  decision='discard';view.rerender(React.createElement(Editor,props(b,1)))
  await screen.findByText('Customer: '+b.customer_key,{}, {timeout:15000});await screen.findByText('Profile version: 1',{}, {timeout:15000})
  await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  assert.ok(oldSignal.aborted);release();await new Promise(resolve=>setImmediate(resolve));assert.equal(saves,0)
  assert.equal(screen.getByLabelText('Module block').value,'Panel')
  assert.deepEqual((await rpc(a,'load',a)).application_record,first.applied_profile)
  assert.deepEqual((await rpc(b,'load',b)).application_record,second.applied_profile)
  fireEvent.change(screen.getByLabelText('Module block'),{target:{value:'PANEL'}})
  decision='stay';const bNext={...b,drawing_key:'b:v2'};view.rerender(React.createElement(Editor,props(bNext,2)))
  await screen.findByText('Profile edits kept. Staying on the current drawing.');assert.equal(screen.getByLabelText('Module block').value,'PANEL')
  assert.equal(guardCalls.at(-1).from.drawingKey,'b:v1');assert.equal(guardCalls.at(-1).to.drawingKey,'b:v2')
  decision='discard';view.rerender(React.createElement(Editor,props(bNext,3)))
  await waitFor(()=>assert.equal(screen.getByLabelText('Module block').value,'Panel'),{timeout:15000})
  console.log('PASS: injected leave guard stays with dirty draft, discard switches customer/drawing and late old save cannot write destination')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
