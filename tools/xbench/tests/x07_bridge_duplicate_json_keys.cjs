const assert=require('node:assert/strict')
const {rpc,ctx,copies,strictJSON,component,React,render,screen,fireEvent,waitFor,cleanup,dispose}=require('./x07_adapter_jsx_journey.cjs')
const Editor=component('X07CustomerProfileEditor')
async function main(){try{
  const context=ctx('duplicate-json');const first=await rpc(context,'confirm',context,{...copies[0],expected_version:0})
  assert.throws(()=>strictJSON('{"customer_key":1,"customer_\\u006bey":2}'),error=>error.code==='BRIDGE_DUPLICATE_JSON_KEY')
  for(const fixture of [
    {duplicate_request_key:'customer_key'}, {duplicate_request_key:'expected_version'}, {duplicate_request_key:'block'},
    {reply:'duplicate_reply_customer'},{reply:'duplicate_reply_version'},{reply:'duplicate_reply_role'}]){
    let errorCode,saveCount=0
    render(React.createElement(Editor,{customerKey:context.customer_key,drawingKey:context.drawing_key,applicationRecord:first.applied_profile,
      loadProfile:(customer,opts)=>rpc(context,'editor_load',context,customer,opts.signal),
      saveProfile:async(customer,roles,opts)=>{saveCount++;try{return await rpc(context,'editor_save',context,
        {customer,roles,expected_version:opts.expected_version},opts.signal,null,fixture.reply||null,fixture)}catch(error){errorCode=error.code;throw error}}}))
    await screen.findByText('Profile version: 1',{}, {timeout:15000})
    await waitFor(()=>assert.ok(!screen.getByRole('button',{name:'Save profile'}).disabled))
    fireEvent.change(screen.getByLabelText('Module block'),{target:{value:'pAnEl'}})
    fireEvent.click(screen.getByRole('button',{name:'Save profile'}))
    await screen.findByRole('alert',{}, {timeout:15000})
    assert.equal(errorCode,'BRIDGE_DUPLICATE_JSON_KEY');assert.equal(saveCount,1)
    assert.equal(screen.getByLabelText('Module block').value,'pAnEl')
    assert.equal((await rpc(context,'editor_load',context,context.customer_key)).version,1)
    assert.deepEqual((await rpc(context,'load',context)).application_record,first.applied_profile)
    cleanup()
  }
  console.log('PASS: duplicate customer/version/nested role keys refuse requests and replies before writes; actual editor drafts and pins retained')
}finally{await dispose()}}
main().catch(error=>{console.error(error);process.exitCode=1})
