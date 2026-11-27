const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

const script = ["profile.js", "native-checkout.js"].map(file =>
  fs.readFileSync(path.join(__dirname, "../extension", file), "utf8")).join("\n");
const origin = "https://www.vinted.co.uk";
const task = {
  id:5, listing_url:origin+"/items/123-jacket", listing_id:"123", vinted_username:"shopper",
  deadline:Date.now()/1000+300, alert_limits:{item_pence:1200,subtotal_pence:1300,total_pence:1700},
  preferences:{delivery_mode:"pickup",pickup_id:"gb:sw1a1aa:corner shop",preferred_courier:"InPost",
    allowed_couriers:["Evri"],allow_same_shop_fallback:true,payment_label:"Visa ending in 4242",
    max_item_price:"12.00",max_shipping:"3.50",max_total:"17.00"}
};
const header = (name="shopper") =>
  '<header><nav><a aria-label="My profile" href="/member/35-'+name+'">My profile</a></nav></header>';
const listingLink = '<a href="/items/123-jacket">Listed jacket</a>';
const rows = {
  "Item price":"£12.00", Shipping:"£3.00", "Buyer protection fee":"£1.00", Total:"£16.00",
  "Delivery method":"Pickup point", "Pickup point name":"Corner Shop","Pickup postcode":"SW1A 1AA",
  Courier:"InPost", "Payment method":"Visa •••• 4242"
};
const row = (k,v) => '<dt>'+k+'</dt><dd>'+v+'</dd>';
const fixture = (changes={}, extra="", account="shopper", link=listingLink) =>
  header(account)+'<main><h1>Checkout</h1>'+link+'<dl>'+
  Object.entries({...rows,...changes}).map(([k,v])=>row(k,v)).join("")+
  '</dl>'+extra+'<button id="pay">Pay</button></main>';
(async () => {
  const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE || undefined,
    args:["--disable-gpu","--no-sandbox","--single-process"]});
  try {
    const context=await browser.newContext();
    const page=await context.newPage();
    let contents="";
    await page.route("**/*", route=>route.request().url().startsWith(origin+"/")
      ? route.fulfill({status:200,contentType:"text/html; charset=utf-8",body:contents})
      : route.abort());
    async function show(html,url="/checkout/order"){
      contents=html;
      await page.goto(origin+url);
      await page.addScriptTag({content:script});
      await page.evaluate(()=>{
        window.clicks=0;
        document.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>window.clicks++));
      });
    }
    const call=(command,details=task)=>page.evaluate(([c,t])=>nativeCheckout(c,t),[command,details]);
    const clicks=()=>page.evaluate(()=>window.clicks);
    async function acceptedQuote(){
      const result=await call("quote");
      assert.equal(result.kind,"quote");
      return {...task,quote:{...result.quote,expires_at:task.deadline}};
    }
    async function mustBlock(html,alter={}){
      await show(html);
      const result=await call("quote",{...task,...alter});
      assert.equal(result.kind,"blocked",JSON.stringify(result));assert.equal(await clicks(),0);
    }
    await show(fixture());
    const diagnosticTask={listing_url:task.listing_url,listing_id:task.listing_id,vinted_username:task.vinted_username};
    let report=await call("diagnostic",diagnosticTask);
    assert.deepEqual(report.missing,[]);
    assert.equal(Object.fromEntries(report.details).Total,"£16.00");
    assert.equal(Object.fromEntries(report.details).Payment,"Visa •••• 4242");
    assert.equal(await clicks(),0);
    assert.equal(await page.evaluate(()=>typeof globalThis.finderNativeReview),"undefined");
    assert.equal((await call("commit",{...task,quote:{expires_at:task.deadline}})).kind,"blocked");
    assert.equal(await clicks(),0);
    await show(fixture({"Payment method":"4111 1111 1111 1111","Pickup postcode":"",Total:""}));
    report=await call("diagnostic",diagnosticTask);
    assert.equal(report.missing.length,3);
    assert.match(report.missing.join(" "),/GBP amount for total/);
    assert.match(report.missing.join(" "),/UK postcode/);
    assert.match(report.missing.join(" "),/masked card/);
    assert.doesNotMatch(JSON.stringify(report),/4111|1111 1111/);assert.equal(await clicks(),0);
    await show(fixture({},"","otherbuyer"));
    report=await call("diagnostic",diagnosticTask);assert.equal(report.kind,"account_changed");
    assert.equal(report.details,undefined);assert.equal(await clicks(),0);
    assert.equal(await page.evaluate(()=>typeof globalThis.finderNativeReview),"undefined");
    await show(fixture({},'<a href="/items/124-bag">Another item</a>'));
    assert.equal((await call("diagnostic",diagnosticTask)).kind,"blocked");assert.equal(await clicks(),0);
    await show(fixture());
    const options=await context.newPage();
    const settingsFile=name=>fs.readFileSync(path.join(__dirname,"../extension",name),"utf8");
    await options.setContent(settingsFile("native.html").replace(/<script[^>]*><\/script>/g,""));
    await options.exposeFunction("inspectFixture",t=>call("diagnostic",t));
    await options.evaluate(()=>{
      window.permissionsRequested=0;
      window.chrome={storage:{local:{get:async()=>({nativePairing:{origin:"https://pilot.example",token:"pilot-test",pilot_username:"pilot",vinted_username:"shopper"}})}},
        tabs:{query:async()=>[{id:22,url:"https://www.vinted.co.uk/checkout/open"}]},
        scripting:{executeScript:async opts=>opts.files?[{documentId:"testdocument"}]:[{result:await opts.func(...opts.args)}]},
        permissions:{request:()=>{window.permissionsRequested++;throw Error("No new grant expected");}}};
      window.nativeCheckout=async(command,task)=>{
        if(command!=="diagnostic")throw Error("Unexpected writable operation");
        return await window.inspectFixture(task);
      };
      window.fetch=()=>{throw Error("No network expected");};
    });
    await options.addScriptTag({content:settingsFile("native-setup.js")});
    await options.locator("#listing").fill("https://www.vinted.co.uk/items/123-jacket");
    await options.getByRole("button",{name:"Check this checkout without paying"}).click();
    await options.locator("#inspect-status").getByText(/read all checkout fields/).waitFor();
    assert.match(await options.locator("#inspect-details").innerText(),/Corner Shop/);
    assert.equal(await clicks(),0);assert.equal(await options.evaluate(()=>window.permissionsRequested),0);
    await show(fixture({},"","otherbuyer"));
    await options.getByRole("button",{name:"Check this checkout without paying"}).click();
    await options.locator("#inspect-status").getByText(/different Vinted account/).waitFor();
    assert.equal(await options.locator("#inspect-details").innerText(),"");
    assert.equal(await clicks(),0);assert.equal(await options.evaluate(()=>window.permissionsRequested),0);
    await options.close();
    await show(header()+'<main><h1>Jacket</h1><button>Buy now</button></main>',"/items/123-jacket");
    assert.equal((await call("open")).kind,"opened");assert.equal(await clicks(),1);
    await show(header()+'<main><h1>Jacket</h1><button>Buy now</button></main>',"/items/124-jacket");
    assert.equal((await call("open")).kind,"blocked");assert.equal(await clicks(),0);

    await show(fixture());
    const accepted=await acceptedQuote();
    assert.deepEqual(accepted.quote,{
      listing_id:"123",currency:"GBP",item_pence:1200,buyer_fee_pence:100,shipping_pence:300,total_pence:1600,
      courier:"InPost",delivery_mode:"pickup",pickup_id:"gb:sw1a1aa:corner shop",pickup_label:"Corner Shop · SW1A 1AA",
      home_address_ref:"",payment_label:"Visa •••• 4242",expires_at:task.deadline});
    assert.equal((await call("commit",accepted)).kind,"clicked");
    assert.equal((await call("commit",accepted)).kind,"blocked");
    assert.equal(await clicks(),1);
    await mustBlock(fixture({},'<dl>'+row("Total","£16.00")+"</dl>"));
    await mustBlock(fixture({Total:"£16.01"}));
    await mustBlock(fixture({Total:"€16.00"}));
    await mustBlock(fixture({"Pickup postcode":""}));
    await mustBlock(fixture({Courier:"Unapproved"}));
    await mustBlock(fixture({"Payment method":"Visa •••• 1111"}));
    await mustBlock(fixture({},'<a href="/items/124-hat">Another item</a>'));
    await mustBlock(fixture({}, "", "shopper", '<a href="/items/124-bag">Different item</a>'));
    await mustBlock(fixture(),{deadline:Date.now()/1000-1});
    await mustBlock(fixture(),{alert_limits:{item_pence:1100}});
    await mustBlock(fixture(),{alert_limits:{item_pence:1200,subtotal_pence:1250}});
    await mustBlock(fixture(),{preferences:{...task.preferences,max_total:"15.99"}});
    await show(fixture({}, "", "someoneelse"));
    assert.equal((await call("quote")).kind,"account_changed");
    await show(fixture({},'<dialog open aria-modal="true">Verify your order</dialog>'));
    assert.equal((await call("quote")).kind,"action_required");
    await show(fixture());let latest=await acceptedQuote();
    await page.locator("dt").getByText("Shipping",{exact:true}).evaluate(el=>el.nextElementSibling.textContent="£3.01");
    await page.locator("dt").getByText("Total",{exact:true}).evaluate(el=>el.nextElementSibling.textContent="£16.01");
    assert.equal((await call("commit",latest)).kind,"blocked");assert.equal(await clicks(),0);
    await show(fixture());latest=await acceptedQuote();
    await page.keyboard.press("Tab");
    assert.equal((await call("commit",latest)).kind,"blocked");assert.equal(await clicks(),0);
    await show(fixture());latest=await acceptedQuote();
    assert.equal((await call("commit",{...latest,quote:{...latest.quote,expires_at:0}})).kind,"blocked");
    await show(fixture({"Pickup point name":"Other Shop"}));latest=await acceptedQuote();
    assert.equal((await call("commit",latest)).kind,"blocked");assert.equal(await clicks(),0);
    assert.equal((await call("commit",{...latest,pickup_change_confirmed:true})).kind,"clicked");
    await show(fixture({"Pickup point name":"Other Shop"}));latest=await acceptedQuote();
    await page.locator("dt").getByText("Pickup point name",{exact:true}).evaluate(el=>el.nextElementSibling.textContent="Third Shop");
    assert.equal((await call("commit",{...latest,pickup_change_confirmed:true})).kind,"blocked");
    await show(fixture());latest=await acceptedQuote();
    const withoutCap={...latest,alert_limits:{item_pence:1200},preferences:{...task.preferences,max_total:"",max_shipping:""}};
    assert.equal((await call("commit",withoutCap)).kind,"blocked");
    assert.equal((await call("commit",{...withoutCap,terms_confirmed:true})).kind,"clicked");
    await show(header()+'<main><h1>Order confirmed</h1>'+listingLink+'<dl>'+row("Order reference","GB123456")+'</dl></main>',"/orders/456");
    assert.deepEqual(await call("receipt"),{kind:"purchased",order_id:"GB123456"});
    await show(header()+'<main><h1>Order confirmed</h1>'+listingLink+'</main>',"/orders/456");
    assert.equal((await call("receipt")).kind,"unknown");
    console.log("Offline Chromium checkout fixtures passed, including capped quotes, ambiguity, one-use consent, account changes, human takeover and receipt identity.");
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
