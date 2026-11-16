const assert=require('node:assert/strict')
const {rpc,ctx,copies,intake,component,React,render,screen,fireEvent,waitFor,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Editor=component('X07CustomerProfileEditor')
async function main(){try{
  const label='Pánel 雪😀';for(const row of intake.inserts)row.name=label;for(const copy of copies)copy.block=label
  const context=ctx('ime');const initial=await rpc(context,'confirm',context,{...copies[0],expected_version:0});let saves=0
  render(React.createElement(Editor,{customerKey:context.customer_key,applicationRecord:initial.applied_profile,
    loadProfile:(customer,opts)=>rpc(context,'editor_load',context,customer,opts.signal),
    saveProfile:(customer,roles,opts)=>{saves++;return rpc(context,'editor_save',context,{customer,roles,expected_version:opts.expected_version},opts.signal)}}))
  await screen.findByText('Profile version: 1',{}, {timeout:15000});await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  const input=screen.getByLabelText('Module block'),form=input.closest('form')
  input.focus();fireEvent.compositionStart(input,{data:''});fireEvent.compositionUpdate(input,{data:'pánel 雪'})
  fireEvent.change(input,{target:{value:'pánel 雪'}})
  assert.equal(fireEvent.keyDown(input,{key:'Enter',code:'Enter',isComposing:true,keyCode:229}),false)
  fireEvent.submit(form);await new Promise(resolve=>setImmediate(resolve));assert.equal(saves,0)
  assert.equal((await rpc(context,'editor_load',context,context.customer_key)).version,1)
  fireEvent.change(input,{target:{value:'pánel 雪😀'}});fireEvent.compositionEnd(input,{data:'pánel 雪😀'})
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await screen.findByText('Profile version: 2',{}, {timeout:15000})
  await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  assert.equal(saves,1);assert.equal((await rpc(context,'editor_load',context,context.customer_key)).roles.module.block,label)
  assert.deepEqual((await rpc(context,'load',context)).application_record,initial.applied_profile)
  console.log('PASS: IME start/update/end prevents premature Enter/submit, completed Unicode saves/reloads once and old pin stays')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
