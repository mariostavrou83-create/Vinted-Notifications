/* A real Chromium Home document and normal product navigation with locally
 * fulfilled marketplace fixtures. The simulated extension API routes execution
 * to those actual documents. No Vinted or Telegram requests leave the test. */
const {chromium}=require("playwright");
const fs=require("node:fs"),path=require("node:path"),vm=require("node:vm"),assert=require("node:assert/strict");
const source=file=>fs.readFileSync(path.join(__dirname,"../extension",file),"utf8");
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,
   args:["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"]});
 const context=await browser.newContext();
 const host="https://www.vinted.co.uk";
 const header='<header><button data-testid="user-menu-button"><img width="24" height="24" alt="buyer"></button></header>';
 const tile=id=>'<div data-testid="feed-item"><a href="/items/'+id+'-book">Book '+id+'</a><img width="40" height="50" data-testid="feed-item--image--img" alt="Fairy Book, condition: Very good, 4.50 £, 5.43 £ Includes Vinted fee"></div>';
 const product=id=>id==="779"?"<title>Verify you are human</title><h1>Security check</h1>":header+
   '<main><nav data-testid="breadcrumb"><a href="/catalog/2-books">Books &amp; Media</a></nav><aside><div data-testid="item-page-summary-plugin"><h1>Fairy book</h1><p>Uploaded 25 seconds ago</p></div><p data-testid="item-price">£4.50</p><p data-testid="total-combined-price">£5.43</p><div data-testid="item-attributes-status">Condition<br>Very good</div><div><a href="/member/555">Bookseller</a><span data-testid="seller-last-logged-in">Last online now</span></div></aside></main>';
 const requests=[];
 await context.route("**/*",route=>{
   const u=new URL(route.request().url());
   if(u.origin!==host)return route.abort();
   requests.push(u.pathname);
   return route.fulfill({contentType:"text/html; charset=utf-8",body:u.pathname==="/"?header+"<main>"+tile(100)+"</main>":product(u.pathname.match(/\d+/)?.[0])});
 });
 const home=await context.newPage();
 const pair={origin:"https://pilot.example",token:"fixture-only",vinted_username:"buyer",pilot_username:"test"};
 const events={},pages=new Map([[1,home]]),docs=new Map([[1,"home"]]),session={},local={},orders=[],consent=[];
 const evt=key=>({addListener:listener=>events[key]=listener});
 const area=store=>({get:async k=>({[k]:store[k]}),set:async d=>Object.assign(store,d),remove:async k=>delete store[k]});
 const url=filename=>"chrome-extension://finder-id/"+filename;
 const getTab=async id=>({id,url:pages.get(id).url(),active:id===1});
 const sender={id:"finder-id",url:url("popup.html")};
 const send=(m,from=sender)=>new Promise(resolve=>{if(!events.message(m,from,resolve))resolve(null);});
 async function navigate(id,address){
   await pages.get(id).goto(address);
   docs.set(id,"page-"+address);
   setTimeout(()=>events.update(id,{status:"complete"}),5);
 }
 const chromeApi={storage:{session:area(session),local:area(local),onChanged:evt("storage")},
   runtime:{id:"finder-id",getURL:url,onMessage:evt("message")},
   tabs:{query:async()=>[await getTab(1)],get:getTab,onUpdated:evt("update"),onRemoved:evt("remove"),
     create:async opts=>{assert.equal(opts.active,false);assert.equal(opts.openerTabId,1);assert.equal(pages.size,1);
       pages.set(2,await context.newPage());await navigate(2,opts.url);return getTab(2);},
     update:async(id,opts)=>{assert.equal(id,2);assert.equal(opts.active,false);await navigate(id,opts.url);return getTab(id);}},
   scripting:{executeScript:async({target,files,func,args})=>{
     const tabid=target.tabId,p=pages.get(tabid),doc=docs.get(tabid);
     if(target.documentIds&&!target.documentIds.includes(doc))throw Error("Document changed");
     for(const f of files||[])await p.addScriptTag({content:source(f)});
     const result=func?await p.evaluate("("+func.toString()+")(..."+JSON.stringify(args||[])+")"):undefined;
     return [{documentId:doc,result}];
   }}};
 const world={chrome:chromeApi,URL,Date,crypto,AbortSignal,localState:async()=>pair,isPaired:async()=>true,
   api:async(o,path,t,body)=>{assert.equal(o,pair.origin);assert.equal(path,"/api/native/home-watch");consent.push(body.enabled);},
   fetch:async(u,opts)=>{assert.equal(u,pair.origin+"/api/native/home");orders.push(...JSON.parse(opts.body).items);
     return {ok:true,status:200,json:async()=>({accepted:1})};}};
 vm.createContext(world);vm.runInContext(source("home-watcher.js"),world);
 await home.exposeFunction("forwardFinderMessage",m=>send(m,{id:"finder-id",frameId:0,documentId:"home",tab:{id:1},url:home.url()}));
 await home.goto(host+"/");
 await home.evaluate(()=>{window.chrome={runtime:{sendMessage:m=>window.forwardFinderMessage(m)}};});
 assert.equal((await send({type:"finder-home-start",allowDetails:true})).ok,true);
 assert.deepEqual(requests,["/"]);
 const waitFor=async pred=>{const limit=Date.now()+6500;while(!pred()&&Date.now()<limit)await new Promise(r=>setTimeout(r,80));assert.ok(pred());};
 await home.locator("main").evaluate((n,s)=>n.insertAdjacentHTML("beforeend",s),tile(777));
 await waitFor(()=>orders.length===1);
 assert.equal(orders[0].id,"777");assert.equal(orders[0].seller_id,"555");assert.equal(orders[0].price_pence,450);
 assert.equal(orders[0].condition,"Very good");assert.equal(orders[0].category,"Books & Media");
 assert.deepEqual(requests,["/","/items/777-book"]);
 await home.locator("main").evaluate((n,s)=>n.insertAdjacentHTML("beforeend",s),tile(778));
 // The new rendered card did reach the worker, but the per-device limit blocks navigation.
 await new Promise(r=>setTimeout(r,1400));assert.deepEqual(requests,["/","/items/777-book"]);
 local.finderHomeDetailChecks=[]; // simulate time elapsed between permitted inspections
 await home.locator("main").evaluate((n,s)=>n.insertAdjacentHTML("beforeend",s),tile(779));
 await waitFor(()=>consent.length===2);
 assert.deepEqual(consent,[true,false]);assert.equal(orders.length,1);
 assert.deepEqual(requests,["/","/items/777-book","/items/779-book"]);assert.equal(pages.size,2);
 console.log("Chrome normal navigation passed: quiet initial Home, new undated link to one ordinary tab, exact identity/price/time/seller, throttling and stop at challenge.");
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
