const fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
const lib=require("playwright");
const src=fs.readFileSync(path.join(__dirname,"../extension/capture.js"),"utf8");
(async()=>{
 const chrome=await lib.chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || undefined,args:["--disable-gpu","--no-sandbox","--single-process"]});
 const page=await chrome.newPage();
 const fresh=new Date(Date.now()-44000).toISOString(),old=new Date(Date.now()-181000).toISOString();
 let contents="";
 await page.route("https://www.vinted.co.uk/**",r=>r.fulfill({status:200,contentType:"text/html; charset=utf-8",body:contents}));
 const capture=async (url,html)=>{
   contents=html;await page.goto("https://www.vinted.co.uk"+url);
   return await page.evaluate(src+";captureVisibleListings()");
 };
 const header='<header><nav><button data-testid="user-menu-button">Menu</button></nav></header>';
 const card=(id,date,extra="")=>'<article><h3>Jacket '+id+'</h3><a href="/items/'+id+'-jacket">Open jacket</a><span data-testid="item-price">£7.50</span>'+
   (date?'<div data-testid="uploaded-date">Uploaded <time datetime="'+date+'">recently</time></div>':"")+extra+'</article>';
 let result=await capture("/",header+'<main>'+card(100,fresh)+card(101,old)+card(104,null)+'</main>');
 assert.equal(result.items.length,1);assert.equal(result.items[0].id,"100");
 assert.equal(result.items[0].price_pence,750);assert.equal(result.missingAge,2);
 result=await capture("/items/100-jacket",header+'<main><h1>My jacket</h1><span data-testid="item-price">£7.50</span><dl><dt>Uploaded</dt><dd>1 minute ago</dd></dl></main>');
 assert.equal(result.items.length,1);assert.equal(result.items[0].title,"My jacket");
 const age=result.observed_at-result.items[0].listed_at;assert.ok(age>=120 && age<130);
 result=await capture("/items/100-jacket",header+'<main><h1>Dress</h1><span data-testid="item-price">£7.50</span><p data-testid="description">Uploaded just now</p></main>');
 assert.equal(result.items.length,0);
 result=await capture("/items/100-jacket",header+'<main><div><h1>Dress</h1><span data-testid="item-price">£7.50</span></div><aside>'+card(400,fresh)+'</aside></main>');
 assert.equal(result.items.length,0);
 result=await capture("/",'<main>'+card(100,fresh)+'</main>');assert.match(result.error,/signed-in/);
 result=await capture("/catalog",header+'<main>'+card(100,fresh)+'</main>');assert.match(result.error,/Home/);
 // Exercise initial capture, naturally appended cards, deduplication, missing
 // age and immediate withdrawal of consent on visible account change.
 contents='<header><button data-testid="user-menu-button"><img width="24" height="24" alt="buyer"></button></header><main>'+card(123,fresh)+card(456,null)+'</main>';
 await page.goto(originForHome());
 function originForHome(){return "https://www.vinted.co.uk/";}
 await page.addScriptTag({content:src});
 for(const file of ["profile.js","home-observer.js"])await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,"../extension/"+file),"utf8")});
 await page.evaluate(()=>{window.events=[];window.chrome={runtime:{sendMessage:async m=>{window.events.push(m);return {ok:true};}}};});
 assert.equal(await page.evaluate(()=>beginFinderHomeObserver("buyer","fixture-run",true)),true);
 const tile='<div data-testid="feed-item"><a href="/items/777-book">Book</a><img width="40" height="60" data-testid="feed-item--image--img" alt="The Book, condition: Very good, 4.50 £, 5.43 £ Includes Vinted fee"></div>';
 await page.locator("main").evaluate((n,s)=>n.insertAdjacentHTML("beforeend",s),tile);
 await page.waitForFunction(()=>events.some(e=>e.candidate?.id==="777"));
 assert.equal(await page.evaluate(()=>events.some(e=>e.items?.some(i=>i.id==="123")||e.candidate?.id==="456")),false);
 await page.locator("main").evaluate((n,s)=>n.insertAdjacentHTML("beforeend",s),card(124,fresh));
 await page.waitForFunction(()=>events.some(e=>e.items?.some(i=>i.id==="124")));
 assert.deepEqual(await page.evaluate(()=>events.flatMap(e=>(e.items||[]).map(i=>i.id))),["124"]);
 await page.locator("header img").evaluate(n=>n.alt="other.buyer");
 await page.waitForFunction(()=>events.some(e=>e.type==="finder-home-ended"));
 await page.locator("main").evaluate((n,s)=>n.insertAdjacentHTML("beforeend",s),card(125,fresh));
 assert.equal(await page.evaluate(()=>events.some(e=>e.items?.some(i=>i.id==="125"))),false);
 const realShape=header+'<main><aside><div data-testid="item-page-summary-plugin"><h1>Fairy Book</h1><span>Uploaded 1 min ago</span></div><p data-testid="item-price">£4.50</p><p data-testid="total-combined-price">£5.43</p><dl><div data-testid="item-attributes-status">Condition<br>Very good</div><div data-testid="item-attributes-isbn_nav">ISBN<br>0723243441</div><div data-testid="item-attributes-upload_date">Uploaded<br>1 min ago</div></dl><div><a href="/member/193012">Book seller</a><small data-testid="seller-last-logged-in">Online today</small></div></aside><nav data-testid="breadcrumb"><a href="/catalog/1-books">Books &amp; Media</a></nav></main>';
 result=await capture("/items/777-book",realShape);
 assert.equal(result.items.length,1);
 const [book]=result.items;
 assert.equal(book.price_pence,450);assert.equal(book.seller_id,"193012");assert.equal(book.condition,"Very good");assert.equal(book.category,"Books & Media");assert.equal(book.model,"0723243441");
 result=await capture("/items/777-book",realShape.replaceAll("1 min ago","18 min ago"));
 assert.equal(result.items.length,0);
 console.log("UK detail shape: actual item ask, age abbreviation, public seller ID, condition, ISBN and marketplace category.");
 console.log("Chromium Home watcher: paired profile, initial and naturally appended listings, missing-age rejection, dedup, account-switch stop passed.");
 console.log("Chromium fixtures: viewport Home, mixed ages, opened item, stale/ambiguous/signed-out/catalogue exclusions passed");
 await chrome.close();
})().catch(e=>{console.error(e);process.exit(1)});
