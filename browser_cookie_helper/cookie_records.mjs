const NAMES = Object.freeze(["access_token_web", "refresh_token_web"]);
const DOMAINS = new Set(["vinted.co.uk", ".vinted.co.uk", "www.vinted.co.uk", ".www.vinted.co.uk"]);
const PATHS = Object.freeze({
  access_token_web: "/api/v2/users/current",
  refresh_token_web: "/web/api/auth/refresh",
});
const MESSAGES = Object.freeze({
  missing: "The two UK Vinted login cookies were not found. Sign in to your own account at www.vinted.co.uk in this browser first.",
  ambiguous: "More than one matching login cookie was found. Nothing was copied; use one clear signed-in Vinted session.",
  invalid: "The original login cookie metadata could not be verified. Nothing was copied. Sign in normally to Vinted and try again.",
});

export class CookieExportError extends Error {
  constructor(code) {
    super(MESSAGES[code] || MESSAGES.invalid);
    this.name = "CookieExportError";
    this.code = code;
  }
}

function validPath(path, required) {
  return typeof path === "string" && path.startsWith("/") && path.length <= 2048
    && !/[^\x21-\x7e]/.test(path)
    && (required === path || (required.startsWith(path)
      && (path.endsWith("/") || required.slice(path.length).startsWith("/"))));
}

export function originalAuthCookieRecords(cookies, now = Date.now() / 1000) {
  if (!Array.isArray(cookies) || cookies.length > 4000 || !Number.isFinite(now)) {
    throw new CookieExportError("invalid");
  }
  const matching = cookies.filter(cookie => cookie && NAMES.includes(cookie.name) && DOMAINS.has(cookie.domain));
  const selected = new Map();
  for (const cookie of matching) {
    if (selected.has(cookie.name)) throw new CookieExportError("ambiguous");
    selected.set(cookie.name, cookie);
  }
  if (selected.size !== 2) throw new CookieExportError("missing");
  return NAMES.map(name => {
    const cookie = selected.get(name);
    if (typeof cookie.value !== "string" || !/^[A-Za-z0-9._~+/=-]{16,8192}$/.test(cookie.value)
        || cookie.secure !== true || typeof cookie.hostOnly !== "boolean"
        || typeof cookie.session !== "boolean" || Object.hasOwn(cookie, "partitionKey")
        || (Object.hasOwn(cookie, "partitioned") && cookie.partitioned !== false)
        || (cookie.hostOnly && cookie.domain !== "www.vinted.co.uk")
        || !validPath(cookie.path, PATHS[name])) {
      throw new CookieExportError("invalid");
    }
    const expires = cookie.session ? null : Math.floor(cookie.expirationDate);
    if (!cookie.session && (typeof cookie.expirationDate !== "number" || !Number.isFinite(expires)
        || expires <= now || expires > 253402300799)) {
      throw new CookieExportError("invalid");
    }
    return {
      name, value: cookie.value, domain: cookie.domain, path: cookie.path,
      secure: true, expires,
      domain_specified: !cookie.hostOnly,
      domain_initial_dot: cookie.domain.startsWith("."),
      path_specified: true,
      discard: cookie.session,
    };
  });
}
