/* Real Chromium layout with a synthetic wall clock and local Vinted fixtures.
 * The page can update naturally. Inspecting its cards must not navigate it. */
const {chromium}=require("playwright");
const fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
(async()=>{
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,
    args:["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"]});
  try{
    const page=await browser.newPage();
    const url="https://www.vinted.co.uk/catalog?search_text=coat";
    const visits=[];
    await page.route("**/*",route=>{
      if(route.request().url()!==url)return route.abort();
      visits.push(url);
      return route.fulfill({contentType:"text/html; charset=utf-8",body:'<header><button data-testid="user-menu-button"><img alt="buyer" width=24 height=24></button></header><main></main>'});
    });
    await page.goto(url);
    await page.clock.install();
    await page.evaluate(()=>{
      window.messages=[];
      window.chrome={runtime:{sendMessage:async m=>{messages.push(m);return {ok:true};}}};
      window.addTile=id=>{
        const stamp=new Date(Date.now()-5000).toISOString();
        document.querySelector("main").insertAdjacentHTML("beforeend",'<div data-testid="feed-item"><a href="/items/'+id+'-test">Coat</a><img width="40" height="40" data-testid="feed-item--image--img" alt="Winter coat, condition: Good, 12.00 £, 13.35 £ Includes Vinted fee" src="https://images1.vinted.net/t/01/test.webp"><span data-testid="uploaded">Uploaded <time datetime="'+stamp+'">just now</time></span></div>');
      };
      addTile("101");
    });
    for(const f of ["profile.js","capture.js","home-observer.js"])
      await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,"../extension",f),"utf8")});
    await page.evaluate(()=>{
      const main=document.querySelector("main");
      main.insertAdjacentHTML("afterbegin",'<button id="ordinary">View 3 new results</button>');
      main.insertAdjacentHTML("beforeend",'<article><button>Show new results</button></article><button>Buy now</button>');
      if(finderNewResultsButton()?.id!=="ordinary")throw Error("wrong catalogue button");
      main.insertAdjacentHTML("afterbegin",'<button id="ambiguous">Show new listings</button>');
      if(finderNewResultsButton()!==null)throw Error("ambiguous control accepted");
      document.getElementById("ambiguous").remove();
      document.getElementById("ordinary").remove();
      if(finderNewResultsButton()!==null)throw Error("seller's button accepted");
    });
    await page.evaluate(url=>beginFinderHomeObserver("buyer","watch",false,url),url);
    await page.clock.runFor(10);
    let msgs=await page.evaluate(()=>messages);
    assert.deepEqual(msgs.map(x=>x.type),["finder-search-baseline"]);
    assert.deepEqual(msgs[0].rendered_ids,["101"]);
    await page.evaluate(()=>addTile("102"));
    await page.clock.runFor(4500);
    assert.equal(await page.evaluate(()=>messages.length),1);
    await page.clock.runFor(700);
    msgs=await page.evaluate(()=>messages);assert.equal(msgs.length,2);
    assert.equal(msgs[1].type,"finder-search-items");
    assert.deepEqual(msgs[1].items.map(x=>x.id),["102"]);
    assert.equal(msgs[1].items[0].price_pence,1200);assert.equal(msgs[1].items[0].subtotal_pence,1335);
    assert.equal(msgs[1].items[0].photo_url,"https://images1.vinted.net/t/01/test.webp");
    const first=await page.evaluate(()=>({...finderHomeObserver,stop:null}));
    await page.clock.runFor(5000);
    const second=await page.evaluate(()=>({...finderHomeObserver,stop:null}));
    assert.ok(second.lastSampled>first.lastSampled);assert.equal(second.lastChanged,first.lastChanged);
    await page.clock.runFor(60000);
    assert.ok((await page.evaluate(()=>messages)).some(m=>m.type==="finder-search-items" && !m.items.length));
    await page.evaluate(()=>history.pushState(null,"","/catalog?search_text=other"));
    await page.clock.runFor(5100);
    assert.equal((await page.evaluate(()=>messages)).at(-1).type,"finder-search-ended");
    assert.deepEqual(visits,[url]);
    console.log("Chrome 5-second catalogue scan, quiet initial inventory, unchanged-card cadence, public evidence, heartbeat and exact URL stop passed.");
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
