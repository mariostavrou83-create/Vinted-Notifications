const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
test("paired catalogue tabs have independent watch IDs, baseline, URL and stop consent",async()=>{
  const session={},local={},posts=[],listeners={},pages=new Map();
  const store=values=>({get:async k=>({[k]:values[k]}),set:async o=>Object.assign(values,o),remove:async k=>delete values[k]});
  const evt=k=>({addListener:f=>listeners[k]=f});
  const base="https://www.vinted.co.uk/catalog?";
  const urlA=base+"search_text=coat&order=newest_first",urlB=base+"search_text=hat";
  [urlA,urlB,base+"order=newest_first&search_text=coat"].forEach((url,i)=>pages.set(i+1,{id:i+1,url,active:true}));
  let selected=1,name="buyer";
  const pair={origin:"https://finder.example",pilot_username:"test",vinted_username:"buyer",token:"paired-device"};
  const chrome={runtime:{id:"own",getURL:n=>"chrome-extension://own/"+n,onMessage:evt("message")},
    storage:{session:store(session),local:store(local),onChanged:evt("storage")},
    tabs:{query:async()=>[pages.get(selected)],get:async id=>pages.get(id),onUpdated:evt("update"),onRemoved:evt("removed")},
    scripting:{executeScript:async({target,files,func,args})=>files?[{documentId:"doc"+target.tabId}]:
      [{result:args?true:name}]}};
  const ctx={chrome,URL,AbortSignal,crypto,Date,localState:async()=>pair,isPaired:async()=>true,
    api:async(o,endpoint,token,data)=>{
      assert.equal(o,pair.origin);assert.equal(token,pair.token);
      assert.equal(endpoint,"/api/native/search-watch");
      posts.push([endpoint,data]);return {matched:1,watch_id:"watch-"+data.url};},
    fetch:async(u,opts)=>{
      assert.equal(opts.credentials,"omit");assert.equal(opts.headers.Authorization,"Bearer "+pair.token);
      assert.equal(u,pair.origin+"/api/native/search-items");
      posts.push([u,JSON.parse(opts.body)]);
      return {ok:true,status:200,json:async()=>({accepted:0})};}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,"../extension/home-watcher.js"),"utf8"),ctx);
  const popup={id:"own",url:"chrome-extension://own/popup.html"};
  const send=(msg,sender=popup)=>new Promise(resolve=>{if(!listeners.message(msg,sender,resolve))resolve(null);});
  const page=id=>({id:"own",tab:{id},frameId:0,url:pages.get(id).url,documentId:"doc"+id});
  assert.ok((await send({type:"finder-search-start"})).ok);
  selected=2;assert.ok((await send({type:"finder-search-start"})).ok);
  selected=3;assert.equal((await send({type:"finder-search-start"})).ok,false);
  assert.equal(posts.length,2);
  const baseline={type:"finder-search-baseline",runId:session.finderCatalogueWatch1.runId,username:"buyer",rendered_ids:["123"]};
  assert.ok((await send(baseline,page(2))).stop);
  assert.ok((await send(baseline,{...page(1),documentId:"unexpected"})).stop);
  assert.ok((await send(baseline,page(1))).ok);
  const submitted=posts.at(-1)[1];assert.deepEqual(submitted.rendered_ids,["123"]);
  assert.equal(submitted.url,urlA);assert.equal(submitted.watch_id,"watch-"+urlA);assert.equal(submitted.baseline,true);
  name="changed";
  assert.ok((await send({...baseline,type:"finder-search-items",items:[]},page(1))).stop);
  assert.equal(posts.at(-1)[1].watch_id,"watch-"+urlA);assert.equal(posts.at(-1)[1].enabled,false);
  assert.equal(session.finderCatalogueWatch1,undefined);assert.equal(session.finderCatalogueWatch2.url,urlB);
});
