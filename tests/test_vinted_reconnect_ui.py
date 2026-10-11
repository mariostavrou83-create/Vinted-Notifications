"""Run the actual reconnect readiness handler without a browser or live service."""

import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReconnectReadinessBrowserTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node is required for browser-script checks"
    )
    def test_readiness_displays_fixed_runtime_result_without_starting_sign_in(self):
        harness = r"""
const fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor() { this.events={}; this.dataset={}; this.disabled=false; this.hidden=true; this.value=''; this.messages=[]; }
  addEventListener(type, handler) { this.events[type]=handler; }
  set textContent(value) { this.messages.push(value); this.text=value; }
  get textContent() { return this.text || ''; }
  set innerHTML(_value) { throw Error('Unsafe HTML insertion'); }
}
function setup(response, statusCode=200) {
  const elements=Object.fromEntries(['status','browser','frame','text','readiness','start','input-form','refresh','cancel','finish'].map(name=>[`[data-${name}]`,new Element()]));
  const root={dataset:{csrf:'offline-csrf'},querySelector:s=>elements[s],querySelectorAll:()=>[]};
  const calls=[];
  async function fetch(url, options) {
    calls.push({url,options});
    assert.equal(url,'/vinted/reconnect/readiness');
    assert.equal(options.method,'POST'); assert.equal(options.credentials,'same-origin');
    assert.equal(options.redirect,'error'); assert.equal(options.cache,'no-store');
    assert.deepEqual([...options.body.entries()],[['csrf','offline-csrf']]);
    const body=await (typeof response === 'function' ? response() : response);
    return {ok:statusCode>=200 && statusCode<300,status:statusCode,json:async()=>body};
  }
  const window={addEventListener() {}};
  new Function('document','window','fetch','setTimeout','clearTimeout','navigator',source)(
    {querySelector:s=>s==='[data-reconnect]'?root:null},window,fetch,
    ()=>{throw Error('Readiness must not schedule browser polling');},()=>{},
    {sendBeacon(){throw Error('Readiness must not cancel or import a session');}}
  );
  assert.equal(calls.length,0); // No request until the owner clicks.
  assert.equal(elements['[data-start]'].disabled,true);
  const button=elements['[data-readiness]'], status=elements['[data-status]'];
  return {elements,calls,button,status,click:()=>button.events.click({target:button})};
}
async function expect(response, text, statusCode=200) {
  const ui=setup(response,statusCode);
  await ui.click();
  assert.equal(ui.status.textContent,text);
  assert.equal(ui.calls.length,1); assert.equal(ui.button.disabled,false);
  assert.equal(ui.elements['[data-browser]'].hidden,true);
  assert.equal(ui.elements['[data-text]'].value,'');
  const ready=response && response.available===true && response.stage==='ready' && statusCode===200;
  assert.equal(ui.elements['[data-start]'].disabled,!ready);
  if (!ready) {
    await ui.elements['[data-start]'].events.click({target:ui.elements['[data-start]']});
    assert.equal(ui.calls.length,1); // Even a programmatic click cannot bypass a failed check.
  }
  assert(!ui.status.messages.some(value=>value.includes('private-token') || value.includes('<script>')));
  return ui;
}
(async()=>{
  const unknown='The private browser runtime could not be confirmed. Try the check again shortly.';
  await expect({available:true,stage:'ready',message:'private-token',next_seq:999},
    'The private browser runtime is ready. Vinted sign-in still needs to be completed and verified.');
  const failures={
    active:'A private browser session is already active. Finish or cancel it before checking again.',
    dependencies:'The private browser runtime is unavailable: browser dependencies are missing.',
    browser_start:'The private browser runtime could not start.',
    context_start:'The private browser runtime could not create an isolated browser session.',
    page_open:'The private browser runtime could not open a page.',
    timeout:'The private browser runtime check timed out. Try the check again shortly.'
  };
  for (const [stage,text] of Object.entries(failures)) await expect({available:false,stage,message:'private-token'},text);
  await expect({available:false,stage:'browser_start'},failures.browser_start,503);
  for (const body of [null,{},'ready',{available:false,stage:'ready'},{available:true,stage:'dependencies'},
    {available:'true',stage:'ready'},{available:false,stage:'<script>private-token</script>',message:'private-token'}]) {
    await expect(body,unknown);
  }
  // Dashboard auth failures still use their fixed route message, not runtime availability.
  await expect({state:'waiting',message:'Dashboard authentication is temporarily unavailable.'},
    'Dashboard authentication is temporarily unavailable.',503);
  await expect({message:'Sign in to continue.'},'Sign in to continue.',401);
  const retry=setup(()=>{throw Error('Offline connection failed');});
  await retry.click(); assert.equal(retry.status.textContent,'Offline connection failed');
  assert.equal(retry.button.disabled,false); assert.equal(retry.calls.length,1);
  // A slow check disables duplicate clicks and replaces its checking message on completion.
  let complete;
  const slow=setup(()=>new Promise(resolve=>{complete=resolve;}));
  const first=slow.click();
  assert.equal(slow.button.disabled,true);
  assert.equal(slow.status.textContent,'Checking the private browser runtime…');
  await slow.click(); assert.equal(slow.calls.length,1);
  complete({available:false,stage:'timeout'}); await first;
  assert.equal(slow.status.textContent,failures.timeout); assert.equal(slow.button.disabled,false);
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""
        result = subprocess.run(
            [
                "node",
                "-e",
                harness,
                str(ROOT / "web_ui_plugin/static/msj_reconnect.js"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(
        shutil.which("node"), "Node is required for browser-script checks"
    )
    def test_start_requires_ready_and_readiness_does_not_disturb_active_view(self):
        harness = r"""
const fs=require('fs'), assert=require('assert');
class Element {
  constructor() {this.events={};this.dataset={};this.disabled=false;this.hidden=true;this.value='';this.textContent='';}
  addEventListener(type,fn) {this.events[type]=fn;}
  set src(value) {this.source=value;queueMicrotask(()=>this.onload());}
  removeAttribute(name) {assert.equal(name,'src');this.source=null;}
}
const nodes=Object.fromEntries(['status','browser','frame','text','readiness','start','input-form','refresh','cancel','finish'].map(name=>[`[data-${name}]`,new Element()]));
const root={dataset:{csrf:'offline-csrf'},querySelector:s=>nodes[s],querySelectorAll:()=>[]};
const calls=[],checks=[{available:false,stage:'browser_start'},{available:true,stage:'ready'},{available:true,stage:'ready'}];
async function fetch(url,options) {
  calls.push(url); assert.equal(options.credentials,'same-origin'); assert.equal(options.redirect,'error');
  if (url.endsWith('/frame')) return {ok:true,headers:{get:()=> '1'},blob:async()=>({offline:true})};
  assert.equal(options.method,'POST'); assert.deepEqual([...options.body.entries()],[['csrf','offline-csrf']]);
  const body=url.endsWith('/readiness')?checks.shift():url.endsWith('/start')?{state:'active',next_seq:1}:
    url.endsWith('/cancel')?{state:'cancelled',message:'Private browser closed.'}:null;
  assert(body,'No verification, credential import, or purchase request is permitted in this test');
  return {ok:true,status:200,json:async()=>body};
}
let frameURLs=0,revoked=0;
new Function('document','window','fetch','setTimeout','clearTimeout','URL','navigator',fs.readFileSync(process.argv[1],'utf8'))(
  {querySelector:()=>root},{addEventListener(){}},fetch,()=>1,()=>{},
  {createObjectURL:()=>{frameURLs++;return 'blob:offline-frame';},revokeObjectURL:()=>{revoked++;}},
  {sendBeacon(){throw Error('Unexpected background request');}}
);
const start=nodes['[data-start]'],readiness=nodes['[data-readiness]'],cancel=nodes['[data-cancel]'];
const click=node=>node.events.click({target:node});
(async()=>{
  assert.equal(start.disabled,true); await click(start); assert.equal(calls.length,0);
  await click(readiness); assert.equal(start.disabled,true); await click(start); assert.equal(calls.length,1);
  await click(readiness); assert.equal(start.disabled,false);
  await click(start); assert.equal(start.disabled,true); assert.equal(nodes['[data-browser]'].hidden,false);
  assert.deepEqual(calls,['/vinted/reconnect/readiness','/vinted/reconnect/readiness','/vinted/reconnect/start','/vinted/reconnect/frame']);
  const activeMessage=nodes['[data-status]'].textContent;
  await click(readiness); await click(start);
  assert.equal(calls.length,4); assert.equal(nodes['[data-browser]'].hidden,false);
  assert.equal(nodes['[data-status]'].textContent,activeMessage); assert.equal(frameURLs,1); assert.equal(revoked,0);
  await click(cancel); assert.equal(nodes['[data-browser]'].hidden,true); assert.equal(revoked,1);
  await click(start); assert.equal(calls.length,5); // A fresh check is required after closing.
  await click(readiness); assert.equal(start.disabled,false); assert.equal(calls.length,6);
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""
        result = subprocess.run(
            [
                "node",
                "-e",
                harness,
                str(ROOT / "web_ui_plugin/static/msj_reconnect.js"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_requires_availability_before_open_and_qualifies_verification(self):
        class Buttons(HTMLParser):
            start = None

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "button" and "data-start" in attributes:
                    self.start = attributes

        reconnect = (ROOT / "web_ui_plugin/templates/msj_reconnect.html").read_text()
        buyer = (ROOT / "web_ui_plugin/templates/msj_buyer.html").read_text()
        buttons = Buttons()
        buttons.feed(reconnect)
        self.assertIn("disabled", buttons.start)
        self.assertIn("First choose", reconnect)
        self.assertIn(
            "Availability alone does not verify your Vinted account", reconnect
        )
        self.assertIn("successful same-account verification", reconnect)
        self.assertIn("private browser is unavailable", buyer)
        self.assertIn("same-account verification succeeds", buyer)
        self.assertNotIn("You do not need to copy access or refresh tokens", buyer)


if __name__ == "__main__":
    unittest.main()
