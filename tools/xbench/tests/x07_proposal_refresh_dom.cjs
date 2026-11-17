const assert=require('node:assert/strict')
const {rpc,ctx,copies,intake,component,React,render,screen,fireEvent,waitFor,cleanup,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Confirmation=component('X07RoleConfirmation')
const row=block=>({block,layer:'MODULES',count:3,insertion_bounds:[0,0,2,0]})
const a=row('Panel'),b=row('Other panel')
const proposal=rows=>({id:'same-drawing',module:{first_choice:rows[0],candidates:rows}})
async function main(){try{
  intake.inserts.push(...[0,1,2].map(x=>({name:'Other panel',layer:'MODULES',x,y:0})))
  copies.push({block:'Other panel',layer:'MODULES',design_window:[0,0,2,0],oracle_count:3})
  const context=ctx('refresh');let calls=[],release,signal
  const confirm=async(value,opts)=>{calls.push(value);signal=opts.signal;await new Promise(done=>{release=done});
    if(opts.signal.aborted)return;return rpc(context,'confirm',context,{...value,expected_version:0},opts.signal)}
  const props=rows=>({proposal:proposal(rows),onConfirm:confirm})
  const view=render(React.createElement(Confirmation,props([a,b])))
  fireEvent.click(screen.getByRole('radio',{name:/Other panel/}))
  view.rerender(React.createElement(Confirmation,props([b,a])))
  assert.ok(screen.getByRole('radio',{name:/Other panel/}).checked);assert.equal(calls.length,0)
  fireEvent.click(screen.getByRole('button',{name:'Confirm roles'}));await waitFor(()=>assert.equal(calls.length,1))
  view.rerender(React.createElement(Confirmation,props([a])))
  assert.ok(signal.aborted);assert.equal(screen.queryByRole('radio',{name:/Other panel/}),null)
  release();await new Promise(resolve=>setImmediate(resolve))
  assert.equal((await rpc(context,'load',context)).expected_version,0)
  assert.equal(screen.queryByText('Roles confirmed.'),null)
  // Renewed explicit confirmation is necessary after replacement, not a saved
  // selection index from the removed candidate.
  view.rerender(React.createElement(Confirmation,{proposal:proposal([a]),onConfirm:(value,opts)=>rpc(context,'confirm',context,{...value,expected_version:0},opts.signal)}))
  assert.equal((await rpc(context,'load',context)).expected_version,0)
  fireEvent.click(screen.getByRole('button',{name:'Confirm roles'}))
  await screen.findByText('Roles confirmed.',{}, {timeout:15000})
  const pin=(await rpc(context,'load',context)).application_record
  assert.equal(pin.roles.module.block,'Panel');assert.equal(pin.version,1)
  cleanup()
  console.log('PASS: proposal reorder preserves role identity, replacement retires pending choice and renewed confirmation gates real adapter write')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
