const assert=require('node:assert/strict')
const {rpc,ctx,copies,intake,component,React,render,screen,fireEvent,waitFor,cleanup,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Confirmation=component('X07RoleConfirmation'),Editor=component('X07CustomerProfileEditor')
async function main(){try{
  const label='<img src=x onerror="globalThis.x07Executed=1"><script>x07Executed=1</script>\u202E雪',layer='<b>MODULES</b>\u2066M\u2069'
  for(const row of intake.inserts){row.name=label;row.layer=layer}for(const copy of copies){copy.block=label;copy.layer=layer}
  const context=ctx('literal-labels');let attempts=0
  const row={block:label,layer,count:3,insertion_bounds:[0,0,2,0]}
  render(React.createElement(Confirmation,{proposal:{module:{first_choice:row,candidates:[row]}},onConfirm:(value,opts)=>{
    if(++attempts===1)return Promise.reject(new Error('<script>globalThis.x07Executed=2</script>\u202E retry'))
    return rpc(context,'confirm',context,{...value,expected_version:0},opts.signal)}}))
  const radio=screen.getByRole('radio');assert.ok(radio.closest('label').textContent.includes(label))
  fireEvent.click(screen.getByRole('button',{name:'Confirm roles'}));await screen.findByRole('alert')
  assert.ok(screen.getByRole('alert').textContent.includes('<script>'))
  assert.equal(document.querySelector('img,script,b'),null);assert.equal(globalThis.x07Executed,undefined)
  await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Confirm roles'}).disabled))
  fireEvent.click(screen.getByRole('button',{name:'Confirm roles'}));await screen.findByText('Roles confirmed.',{}, {timeout:15000});cleanup()
  const pin=(await rpc(context,'load',context)).application_record
  render(React.createElement(Editor,{customerKey:context.customer_key,applicationRecord:pin,
    loadProfile:(customer,opts)=>rpc(context,'editor_load',context,customer,opts.signal),
    saveProfile:(customer,roles,opts)=>rpc(context,'editor_save',context,{customer,roles,expected_version:opts.expected_version},opts.signal)}))
  await screen.findByText('Profile version: 1',{}, {timeout:15000});await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  assert.equal(screen.getByLabelText('Module block').value,label);assert.equal(screen.getByLabelText('Module layer').value,layer)
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await screen.findByText('Profile version: 2',{}, {timeout:15000})
  await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  assert.equal((await rpc(context,'editor_load',context,context.customer_key)).roles.module.block,label)
  assert.equal(document.querySelector('img,script,b'),null);assert.equal(globalThis.x07Executed,undefined)
  console.log('PASS: HTML/script/bidirectional CAD labels and errors stay literal accessible text, no markup execution, real save/reload unchanged')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
