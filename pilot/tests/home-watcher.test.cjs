const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,"../extension/home-watcher.js"),"utf8");
function harness(){
  const origin="https://pilot.example";
  const pair={origin,pilot_username:"tester",vinted_username:"buyer",token:"fixture-device-only"};
  const values={},posted=[];
  const session={get:async key=>({[key]:values[key]}),set:async obj=>Object.assign(values,obj),remove:async key=>delete values[key]};
  const stored={};
  const local={get:async key=>({[key]:stored[key]}),set:async obj=>Object.assign(stored,obj),remove:async key=>delete stored[key]};
  const listeners={};
  const on=name=>({addListener:fn=>listeners[name]=fn});
  const tab={id:123,url:"https://www.vinted.co.uk/",active:true};
  const popup={id:"own",url:"chrome-extension://own/popup.html"};
  let currentName="buyer";
  const chrome={
    storage:{session,local,onChanged:on("storage")},runtime:{id:"own",getURL:path=>"chrome-extension://own/"+path,onStartup:on("startup"),onMessage:on("message")},
    tabs:{query:async()=>[tab],get:async()=>tab,onUpdated:on("nav"),onRemoved:on("remove")},
    scripting:{executeScript:async({files,args})=>{
      if(files)return [{documentId:"doc-home"}];
      if(!args)return [{result:currentName}];
      return [{result:true}];
    }}
  };
  const globals={chrome,URL,crypto,AbortSignal,localState:async()=>pair,isPaired:async()=>true,
    api:async(o,path,token,body)=>{assert.equal(o,origin);assert.equal(token,pair.token);posted.push([path,body]);},
    fetch:async(u,opts)=>{assert.equal(u,origin+"/api/native/home");assert.equal(opts.headers.Authorization,"Bearer "+pair.token);
      assert.equal(opts.credentials,"omit");posted.push(["home",JSON.parse(opts.body)]);return {ok:true,status:200,json:async()=>({accepted:1})};}
  };
  vm.createContext(globals);vm.runInContext(source,globals);
  function send(msg,sender=popup){
    return new Promise(resolve=>{if(!listeners.message(msg,sender,resolve))resolve(null);});
  }
  const page={id:"own",url:tab.url,documentId:"doc-home",tab:{id:tab.id},frameId:0};
  return {send,page,posted,tab,stored,restart:async()=>{delete values.finderHomeWatch;listeners.startup();await new Promise(r=>setTimeout(r,10));},changeName:name=>currentName=name,homeItems:()=>({
    type:"finder-home-items",runId:values.finderHomeWatch?.runId,username:"buyer",items:[{id:"3"}]
  })};
}
test("requires deliberate popup, device binding, exact document and matching account",async()=>{
 const h=harness();
 assert.equal(await h.send({type:"finder-home-start"},{id:"other",url:"chrome-extension://other/popup.html"}),null);
 assert.equal((await h.send({type:"finder-home-start"},h.page)).stop,true);
 assert.equal((await h.send({type:"finder-home-start"})).ok,true);
 assert.equal(h.posted[0][0],"/api/native/home-watch");assert.equal(h.posted[0][1].enabled,true);
 assert.equal((await h.send(h.homeItems(),{...h.page,documentId:"another-document"})).stop,true);
 assert.equal(h.posted.length,1);
 assert.equal((await h.send(h.homeItems(),h.page)).ok,true);assert.equal(h.posted[1][0],"home");
 assert.equal((await h.send({type:"finder-home-state"})).accepted,1);
 h.changeName("other");
 assert.equal((await h.send(h.homeItems(),h.page)).stop,true);
 assert.equal(h.posted.at(-1)[1].enabled,false);
 assert.equal((await h.send({type:"finder-home-state"})).watching,false);
});
test("foreign site and unconfirmed username cannot enable Home",async()=>{
 const h=harness();h.tab.url="https://other.example/";
 assert.equal((await h.send({type:"finder-home-start"})).ok,false);
 h.tab.url="https://www.vinted.co.uk/";
 h.changeName("");
 assert.equal((await h.send({type:"finder-home-start"})).ok,false);
 assert.equal(h.posted.length,0);
});

test("restart restores only a remembered, visible, same-profile Home; explicit stop forgets it",async()=>{
  const h=harness();
  await h.restart(); assert.equal(h.posted.length,0);
  await h.send({type:"finder-home-start",remember:true});
  assert.equal(h.stored.finderHomeResume.username,"buyer");
  h.changeName("other"); await h.restart();
  assert.equal((await h.send({type:"finder-home-state"})).watching,false);
  h.changeName("buyer"); await h.restart();
  assert.equal((await h.send({type:"finder-home-state"})).watching,true);
  await h.send({type:"finder-home-stop"});await h.restart();
  assert.equal((await h.send({type:"finder-home-state"})).watching,false);
  assert.equal(h.stored.finderHomeResume,undefined);
});
