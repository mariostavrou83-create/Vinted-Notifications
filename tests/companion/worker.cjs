"use strict";
const assert=require("node:assert/strict"), fs=require("node:fs"), path=require("node:path"),vm=require("node:vm");
function storage(initial={}) {
  const state={...initial};
  return {state,setAccessLevel:async()=>{},async get(keys){return Object.fromEntries((Array.isArray(keys)?keys:[keys]).map(k=>[k,state[k]]));},
    async set(data){Object.assign(state,data);},async remove(key){delete state[key];}};
}
async function test() {
  const local=storage(),session=storage(),requests=[],ev={addListener(){}},base="https://finder.example";
  const extension="chrome-extension://unit-test/", home="https://www.vinted.co.uk/";
  const tab={id:12,active:true,url:home,status:"complete"};
  let accepted=1, serverStatus=200, allowAccount=true,grant=false;
  const chrome={
    storage:{local,session},
    runtime:{id:"unit-test",getURL:p=>extension+p,onStartup:ev,onMessage:ev},
    tabs:{onUpdated:ev,onActivated:ev,onRemoved:ev,query:async()=>[tab],get:async()=>tab},
    permissions:{contains:async()=>grant,remove:async()=>{grant=false;}},
    scripting:{executeScript:async req=>{
      if(req.files)return [{documentId:"doc-a"}];
      const src=String(req.func);
      if(src.includes(".identity("))return [{result:{id:"42",name:"demo"}}];
      return [{result:allowAccount}];
    }}
  };
  const result=(data,status=200)=>({ok:status<400,status,json:async()=>data});
  const context=vm.createContext({chrome,URL,AbortSignal,crypto:require("node:crypto").webcrypto,fetch:async(url,options)=>{
    if(url===extension+"dashboard.json")return result({origin:base});
    requests.push({url,...options,body:JSON.parse(options.body)});
    return url.endsWith("/pair")?result({source_id:"src",token:"test-only-source"}):result({accepted},serverStatus);
  }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../../home_companion/worker.js"),"utf8"),context);
  const popup={id:chrome.runtime.id,url:extension+"popup.html"};
  const call=(m,s=popup)=>context.handle(m,s);
  assert.equal((await call({action:"pair",code:"one-use"})).ok,true);
  assert.equal(requests[0].credentials,"omit");
  assert.deepEqual(requests[0].body,{code:"one-use",account_id:"42"});
  assert.equal(local.state.pair.token,"test-only-source");
  assert.equal((await call({action:"watch"})).ok,true);
  const watch=session.state.watch, doc={id:chrome.runtime.id,url:home,tab:{id:12},frameId:0,documentId:"doc-a"};
  const item={id:"101",url:home+"items/101",title:"Skirt",price_gbp:7.5,listed_at:1790000000};
  let packet={action:"observed",run:watch.run,items:[item]};
  assert.equal((await call(packet,{...doc,tab:{id:14}})).stop,true);
  assert.equal(requests.length,1);
  assert.equal((await call(packet,doc)).ok,true);
  assert.deepEqual(requests[1].body,{account_id:"42",items:[item]});
  assert.equal(requests[1].headers.Authorization,"Bearer test-only-source");
  assert.equal(requests[1].redirect,"error");
  allowAccount=false;
  assert.equal((await call(packet,doc)).ok,false);
  assert.equal(requests.length,2);
  allowAccount=true; accepted=0; packet={action:"observed",run:watch.run,items:[]};
  assert.equal((await call(packet,doc)).ok,true);
  serverStatus=401;grant=true;
  assert.equal((await call(packet,doc)).stop,true);
  assert.equal(local.state.pair,undefined);
  assert.equal(grant,false);
  assert.equal(session.state.watch,undefined);
}
test().catch(e=>{console.error(e);process.exitCode=1;});
