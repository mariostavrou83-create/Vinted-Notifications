const {readFileSync}=require("node:fs");
const vm=require("node:vm");
const assert=require("node:assert/strict");
const test=require("node:test");
const path=require("node:path");
const root=path.resolve(__dirname,"../extension");
const source=readFileSync(path.join(root,"profile.js"),"utf8");
const origin="https://www.vinted.co.uk";
function link(href,text,label="",visible=true,inMenu=true) {
  return {href:new URL(href,origin).href,innerText:text,getAttribute:()=>label,closest:selector=>inMenu && selector.includes("navigation") ? {} : null,getClientRects:()=>visible?[{}]:[]};
}
function pick(links,site=origin) {
  const ctx={URL,location:new URL(site),document:{querySelectorAll:()=>links},getComputedStyle:()=>({visibility:"visible"})};
  return vm.runInNewContext(source+"\nreadOwnProfile()",ctx);
}
test("only an explicitly self-labelled visible UK profile yields a selection",()=>{
  assert.equal(pick([link("/member/123-some.seller","some.seller"),link("/member/17-my.buyer","My profile")]),"my.buyer");
  assert.equal(pick([link("/member/123-some.seller","some.seller")]),null);
  assert.equal(pick([link("/member/123","My profile")]),null);
  assert.equal(pick([link("/member/123-seller","My profile","",true,false)]),null);
  assert.equal(pick([link("/member/123","My profile: example_profile")]),"example_profile");
  assert.equal(pick([link("/member/23-mario","My profile","",false)]),null);
  assert.equal(pick([link("/member/23-mario","My profile")],"https://vinted.example"),null);
});
test("ambiguous or conflicting labels and untrusted origins are ignored",()=>{
  assert.equal(pick([link("/member/23-one","My profile"),link("/member/24-two","My profile")]),null);
  assert.equal(pick([link("/member/23-mario","Your profile: different")]),null);
  assert.equal(pick([link("https://other.example/member/23-mario","My profile")]),null);
});
test("manifest limits the extension to deliberate active-tab access",()=>{
  const manifest=JSON.parse(readFileSync(path.join(root,"manifest.json"),"utf8"));
  assert.deepEqual(manifest.permissions.sort(),["activeTab","scripting","storage"]);
  assert.equal(manifest.host_permissions,undefined);
  assert.equal(manifest.content_scripts,undefined);
  assert.equal(manifest.background,undefined);
});
