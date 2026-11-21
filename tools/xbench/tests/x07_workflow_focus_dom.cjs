const assert=require('node:assert/strict')
const {rpc,ctx,copies,component,React,render,screen,fireEvent,waitFor,mount,cleanup,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Editor=component('X07CustomerProfileEditor')
function usable(){const node=document.activeElement;assert.ok(document.contains(node));assert.notEqual(node,document.body);assert.ok(!node.disabled);return node}
async function main(){try{
  const context=ctx('focus');const first=await rpc(context,'confirm',context,{...copies[0],expected_version:0})
  let failures=1
  render(React.createElement(Editor,{customerKey:context.customer_key,applicationRecord:first.applied_profile,
    loadProfile:(customer,opts)=>rpc(context,'editor_load',context,customer,opts.signal),
    saveProfile:(customer,roles,opts)=>rpc(context,'editor_save',context,{customer,roles,expected_version:opts.expected_version},opts.signal,failures--?'save':null)}))
  await screen.findByText('Profile version: 1',{}, {timeout:15000});await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
  fireEvent.click(screen.getByRole('button',{name:'Add roof role'}));assert.equal(usable(),screen.getByLabelText('Roof layer'))
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));const error=screen.getByRole('alert')
  assert.equal(usable(),screen.getByLabelText('Roof layer'))
  assert.equal(document.getElementById(usable().getAttribute('aria-describedby')),error)
  fireEvent.change(screen.getByLabelText('Roof layer'),{target:{value:'ROOF'}})
  fireEvent.click(screen.getByRole('button',{name:'Remove roof role'}));assert.equal(usable(),screen.getByLabelText('Module layer'))
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await screen.findByRole('alert',{}, {timeout:15000})
  await waitFor(()=>assert.equal(usable(),screen.getByRole('button',{name:'Save profile'})))
  assert.equal(document.getElementById(usable().getAttribute('aria-describedby')),screen.getByRole('alert'))
  fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await screen.findByText('Profile version: 2',{}, {timeout:15000})
  await waitFor(()=>assert.equal(usable(),screen.getByRole('button',{name:'Save profile'})));cleanup()
  mount(context);await screen.findByRole('button',{name:'Use copy 1'},{timeout:15000})
  fireEvent.click(screen.getByRole('button',{name:'Cancel workflow'}));assert.equal(usable(),screen.getByRole('status'));cleanup()
  let loads=0
  render(React.createElement(Editor,{customerKey:context.customer_key,
    loadProfile:()=>++loads===1?Promise.reject(new Error('injected load')):rpc(context,'editor_load',context,context.customer_key)}))
  await screen.findByRole('button',{name:'Retry loading profile'})
  assert.equal(usable(),screen.getByRole('button',{name:'Retry loading profile'}))
  fireEvent.click(screen.getByRole('button',{name:'Retry loading profile'}));await screen.findByText('Profile version: 2',{}, {timeout:15000})
  // Retry button unmounts after successful load. Focus must recover to editor.
  await waitFor(()=>assert.equal(usable(),screen.getByLabelText('Module layer')))
  console.log('PASS: focus after validation/removal/save retry/cancel/load retry stays mounted, usable and error description resolves')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
