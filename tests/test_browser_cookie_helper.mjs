import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import {CookieExportError, originalAuthCookieRecords} from "../browser_cookie_helper/cookie_records.mjs";

const NOW = 1793386800;
const SECRET = "private-original-cookie-0123456789";
const valid = (extra = {}) => ["access_token_web", "refresh_token_web"].map(name => ({
  name, value: SECRET + name, domain: ".vinted.co.uk", path: "/", secure: true,
  httpOnly: true, hostOnly: false, session: false, expirationDate: NOW + 3600,
  ...extra,
}));

test("copies exactly the two original records in deterministic order", () => {
  const source = valid().reverse();
  const before = JSON.stringify(source);
  const records = originalAuthCookieRecords(source, NOW);
  assert.deepEqual(records.map(row => row.name), ["access_token_web", "refresh_token_web"]);
  assert.deepEqual(records[0], {
    name: "access_token_web", value: SECRET + "access_token_web", domain: ".vinted.co.uk",
    path: "/", secure: true, expires: NOW + 3600, domain_specified: true,
    domain_initial_dot: true, path_specified: true, discard: false,
  });
  assert.equal(JSON.stringify(source), before);
});

test("retains host-only scope and original refresh path", () => {
  const source = valid({domain: "www.vinted.co.uk", hostOnly: true});
  source[1].path = "/web/api/auth";
  const records = originalAuthCookieRecords(source, NOW);
  assert.equal(records[0].domain, "www.vinted.co.uk");
  assert.equal(records[0].domain_specified, false);
  assert.equal(records[0].domain_initial_dot, false);
  assert.equal(records[1].path, "/web/api/auth");
});

test("session cookies retain session-only expiry", () => {
  const source = valid({session: true});
  source.forEach(cookie => delete cookie.expirationDate);
  const records = originalAuthCookieRecords(source, NOW);
  assert.ok(records.every(record => record.expires === null && record.discard === true));
});

test("does not copy protection cookies or unrelated domains", () => {
  const source = [...valid(),
    {name: "datadome", value: "NEVER-COPY-PROTECTION", domain: ".vinted.co.uk"},
    {name: "anon_id", value: "NEVER-COPY-ANON", domain: ".vinted.co.uk"},
    {...valid()[0], domain: ".unrelated.test", value: "NEVER-COPY-OTHER"},
  ];
  assert.doesNotMatch(JSON.stringify(originalAuthCookieRecords(source, NOW)), /NEVER-COPY/);
});

test("duplicates fail instead of selecting or changing scope", () => {
  const source = [...valid(), {...valid()[0], domain: "www.vinted.co.uk", hostOnly: true}];
  assert.throws(() => originalAuthCookieRecords(source, NOW), error => {
    assert.equal(error.code, "ambiguous");
    assert.doesNotMatch(error.message, /private-original-cookie/);
    return true;
  });
});

test("rejects partitioned, insecure, expired, malformed and inapplicable cookies", () => {
  const invalid = [
    {partitionKey: {topLevelSite: "https://vinted.co.uk"}}, {partitionKey: null},
    {partitioned: true}, {secure: false}, {expirationDate: NOW},
    {expirationDate: Infinity}, {expirationDate: "not-an-expiry"},
    {expirationDate: 253402300800}, {value: "short"}, {value: SECRET + "\n"},
    {hostOnly: "false"}, {session: "false"}, {path: "/not-the-auth-path"},
    {path: "/api/v2/usersX"}, {domain: "vinted.co.uk", hostOnly: true},
  ];
  for (const extra of invalid) {
    const source = valid();
    source[0] = {...source[0], ...extra};
    assert.throws(() => originalAuthCookieRecords(source, NOW), CookieExportError);
  }
});

test("missing names and oversized snapshots fail without private error text", () => {
  for (const source of [null, {}, [], [valid()[0]], Array(4001).fill(valid()[0])]) {
    assert.throws(() => originalAuthCookieRecords(source, NOW), error => {
      assert.ok(error instanceof CookieExportError);
      assert.doesNotMatch(error.message, /private-original-cookie/);
      return true;
    });
  }
});

test("manifest grants no background, storage, content script or remote access", async () => {
  const base = new URL("../browser_cookie_helper/", import.meta.url);
  const manifest = JSON.parse(await readFile(new URL("manifest.json", base), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions, ["cookies", "clipboardWrite"]);
  assert.deepEqual(manifest.host_permissions, ["https://vinted.co.uk/*", "https://www.vinted.co.uk/*"]);
  for (const key of ["background", "content_scripts", "externally_connectable", "web_accessible_resources", "optional_permissions"]) {
    assert.equal(manifest[key], undefined);
  }
  assert.match(manifest.content_security_policy.extension_pages, /connect-src 'none'/);
  const popup = await readFile(new URL("popup.js", base), "utf8");
  assert.doesNotMatch(popup, /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|console|localStorage|sessionStorage)\b/);
  assert.match(popup, /chrome\.cookies\.getAll\(\{domain: "vinted\.co\.uk"\}\)/);
  assert.match(popup, /clipboard\.writeText\(""\)/);
});
