const fs=require('node:fs'),assert=require('node:assert/strict')
const {rpc,ctx,copies,mount,screen,fireEvent,waitFor,cleanup,dispose,resources,protocolStats}=require('./x07_adapter_jsx_journey.cjs')
async function main(){try{
  for(const [fault,code] of [['hung','BRIDGE_TIMEOUT'],['stdout_large','BRIDGE_STDOUT_LIMIT'],['stderr_large','BRIDGE_STDERR_LIMIT']]){
    const context={...ctx('resource-'+fault),customer_key:'synthetic-'+fault}
    const pin=(await rpc(context,'confirm',context,{...copies[0],expected_version:0})).applied_profile
    let attempts=0
    mount(context,{saveProfile:(customer,roles,opts)=>rpc(context,'editor_save',context,
      {customer,roles,expected_version:opts.expected_version},opts.signal,null,++attempts===1?fault:null,{timeout_ms:500})})
    await screen.findByRole('button',{name:'Use copy 1'},{timeout:15000});fireEvent.click(screen.getByRole('button',{name:'Use copy 1'}))
    await screen.findByText('Profile version: 1',{}, {timeout:15000})
    await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
    fireEvent.change(screen.getByLabelText('Module block'),{target:{value:'pAnEl'}})
    const start=Date.now();fireEvent.click(screen.getByRole('button',{name:'Save profile'}))
    await screen.findByRole('alert',{}, {timeout:15000});assert.ok(Date.now()-start<15000)
    assert.equal(screen.getByLabelText('Module block').value,'pAnEl')
    assert.equal(resources().child_count,0)
    const stats=protocolStats.at(-1)
    if(fault!=='hung'){assert.ok(stats.stored_stdout_bytes<=1024*1024);assert.ok(stats.stored_stderr_bytes<=64*1024)}
    await assert.rejects(rpc(context,'editor_load',context,context.customer_key,null,null,fault,{timeout_ms:500}),error=>{
      assert.equal(error.code,code);assert.ok(error.message.length<100);return true})
    const plan=await rpc(context,'load',context);assert.equal(plan.expected_version,1);assert.deepEqual(plan.application_record,pin)
    await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
    fireEvent.click(screen.getByRole('button',{name:'Save profile'}));await screen.findByText('Profile version: 2',{}, {timeout:15000})
    await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled));cleanup()
  }
  console.log('PASS: bounded hung/stdout/stderr refusal, preserved drafts, unchanged pins, retry and owned child cleanup')
}finally{await dispose();assert.equal(resources().child_count,0);assert.equal(fs.existsSync(resources().database),false);assert.equal(fs.existsSync(resources().root),false)}}
main().catch(error=>{console.error(error);process.exitCode=1})
