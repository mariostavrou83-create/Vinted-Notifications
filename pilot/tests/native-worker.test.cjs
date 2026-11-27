const test = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const worker = readFileSync(path.join(__dirname,"../extension/native-worker.js"),"utf8");

function harness() {
  const pair={origin:"https://pilot.example",token:"scoped-pilot-credential",vinted_username:"shopper"};
  const area = initial=>{
    const values={...initial};
    return {get:async key=>({[key]:values[key]}),set:async v=>Object.assign(values,v),remove:async key=>delete values[key]};
  };
  const local=area({nativePairing:pair}),session=area({});
  const event={addListener(){},hasListener(){return true;}};
  let tabs=0,commands=[],posts=[],resultKind="clicked",active=true,wrongDoc=false;
  const now=Date.now()/1000;
  const storedQuote={listing_id:"123",currency:"GBP",item_pence:1000,buyer_fee_pence:100,shipping_pence:200,total_pence:1300,
    courier:"InPost",delivery_mode:"pickup",pickup_id:"gb:sw1a1aa:shop",pickup_label:"Shop · SW1A 1AA",home_address_ref:"",payment_label:"Vinted balance"};
  const base={stage:"quote",id:1,deadline:now+60,task_token:"0123456789012345678901234567",
    listing_url:"https://www.vinted.co.uk/items/123",listing_id:"123",vinted_username:"shopper"};
  let tabUrl=base.listing_url;
  const chrome={storage:{local,session},runtime:{onMessage:event,onStartup:event,onInstalled:event},alarms:{onAlarm:event,clear:async()=>true},
    tabs:{create:async options=>{tabs++;tabUrl=options.url;return {id:tabs};},get:async()=>({status:"complete",url:tabUrl,active})},
    scripting:{executeScript:async ({target,files,args})=>{
      if(target.documentIds && wrongDoc)throw Error("Gone");
      if(files)return [];
      const command=args[0];commands.push(command);
      const result=command==="open"?(tabUrl="https://www.vinted.co.uk/checkout/order",{kind:"opened"})
        :command==="quote"?{kind:"quote",quote:storedQuote}
        :command==="commit"?{kind:resultKind}
        :{kind:"purchased",order_id:"GB12345"};
      return [{documentId:"doc:checkout",result}];
    }}};
  const fetch=async (url,options)=>{
    assert.ok(url.startsWith(pair.origin+"/native/device/"));
    assert.equal(options.credentials,"omit");assert.equal(options.redirect,"error");
    assert.equal(options.headers.Authorization,"Bearer "+pair.token);
    posts.push({url,body:JSON.parse(options.body)});
    return {ok:true,json:async()=>({ok:true})};
  };
  const env={chrome,fetch,AbortSignal,URL,Date,Promise,setTimeout:cb=>setImmediate(cb),importScripts(){}};
  vm.createContext(env);vm.runInContext(worker,env);
  return {handle:env.handle,local,person:pair,commands,posts,base,
    commit:{...base,stage:"commit",task_token:"1123456789012345678901234567",quote:{...storedQuote,expires_at:now+60}},
    setWrongDoc:()=>wrongDoc=true,setInactive:()=>active=false,changeUser:()=>resultKind="account_changed"};
}
test("quoted tab is consumed before submit, receipt is required, and a repeated task cannot click",async()=>{
  const run=harness();await run.handle(run.person,run.base);
  assert.deepEqual(run.commands,["open","quote"]);
  assert.equal(run.posts.at(-1).body.kind,"quote");
  await run.handle(run.person,run.commit);
  assert.deepEqual(run.commands,["open","quote","commit","receipt"]);
  assert.equal(run.posts.at(-1).body.kind,"purchased");assert.equal(run.posts.at(-1).body.order_id,"GB12345");
  await run.handle(run.person,run.commit);
  assert.equal(run.commands.filter(x=>x==="commit").length,1);
  assert.equal(run.posts.at(-1).body.kind,"blocked");
});
test("changed terms, inactive tab, and mismatched document never cause a payment click",async()=>{
  for(const reason of ["amount","tab","doc"]){
    const run=harness();await run.handle(run.person,run.base);
    if(reason==="amount")run.commit.quote.total_pence=2200;
    if(reason==="tab")run.setInactive();
    if(reason==="doc")run.setWrongDoc();
    await run.handle(run.person,run.commit);
    assert.equal(run.commands.includes("commit"),false);
    assert.equal(run.posts.at(-1).body.kind,reason==="doc"?"unknown":"blocked");
  }
});
test("a positively identified different account revokes the pairing on both sides",async()=>{
  const run=harness();await run.handle(run.person,run.base);
  run.changeUser();await run.handle(run.person,run.commit);
  assert.equal((await run.local.get("nativePairing")).nativePairing,undefined);
  assert.equal(run.posts.at(-1).url,run.person.origin+"/native/device/disconnect");
});
test("invalid Vinted listing origin and expired requests do not open a tab",async()=>{
  const run=harness();
  await run.handle(run.person,{...run.base,listing_url:"https://other.example/items/123"});
  await run.handle(run.person,{...run.base,deadline:0});
  assert.equal(run.commands.length,0);assert.equal(run.posts.length,0);
});
