// A stand-in for Google in the smoke tests: a minimal OpenID Connect provider.
// It shows an account picker (an email field), issues real RS256 ID tokens,
// checks PKCE (S256) and the client secret. No dependencies; Node only.
//
//   PUBLIC_URL    where the browser reaches it (authorize page)
//   INTERNAL_URL  where Opsis reaches it (token and keys)
//   CLIENT_ID, CLIENT_SECRET
import { createHash, generateKeyPairSync, randomBytes, sign } from "node:crypto";
import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? 9400);
const PUBLIC_URL = process.env.PUBLIC_URL ?? `http://localhost:${PORT}`;
const INTERNAL_URL = process.env.INTERNAL_URL ?? PUBLIC_URL;
const CLIENT_ID = process.env.CLIENT_ID ?? "smoke-client";
const CLIENT_SECRET = process.env.CLIENT_SECRET ?? "";
const ISSUER = INTERNAL_URL;

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: "jwk" }), kid: "fake-1", alg: "RS256", use: "sig" };
const codes = new Map();

const b64url = (data) => Buffer.from(data).toString("base64url");
const escape = (text) => String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function idToken(claims) {
  const head = b64url(JSON.stringify({ alg: "RS256", typ: "JWT", kid: jwk.kid }));
  const body = b64url(JSON.stringify(claims));
  const signature = sign("RSA-SHA256", Buffer.from(`${head}.${body}`), privateKey);
  return `${head}.${body}.${b64url(signature)}`;
}

function send(res, status, body, type = "application/json") {
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
  res.end(type === "application/json" ? JSON.stringify(body) : body);
}

async function form(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  return Object.fromEntries(new URLSearchParams(raw));
}

createServer(async (req, res) => {
  const url = new URL(req.url, PUBLIC_URL);
  if (url.pathname === "/.well-known/openid-configuration") {
    return send(res, 200, {
      issuer: ISSUER,
      authorization_endpoint: `${PUBLIC_URL}/authorize`,
      token_endpoint: `${INTERNAL_URL}/token`,
      jwks_uri: `${INTERNAL_URL}/jwks`,
      response_types_supported: ["code"],
      code_challenge_methods_supported: ["S256"],
    });
  }
  if (url.pathname === "/jwks") return send(res, 200, { keys: [jwk] });
  if (url.pathname === "/authorize" && req.method === "GET") {
    const p = url.searchParams;
    if (p.get("client_id") !== CLIENT_ID || p.get("code_challenge_method") !== "S256") {
      return send(res, 400, "Bad request: unknown client or no PKCE", "text/plain");
    }
    const hidden = ["redirect_uri", "state", "nonce", "code_challenge"]
      .map((name) => `<input type="hidden" name="${name}" value="${escape(p.get(name) ?? "")}">`)
      .join("");
    return send(
      res,
      200,
      `<!doctype html><title>Fake Google</title><h1>Choose an account</h1>
<form method="post" action="/authorize">${hidden}
<label>Email <input name="email" type="email" required></label>
<button type="submit">Continue</button></form>`,
      "text/html; charset=utf-8",
    );
  }
  if (url.pathname === "/authorize" && req.method === "POST") {
    const f = await form(req);
    const code = randomBytes(16).toString("hex");
    codes.set(code, { email: f.email, nonce: f.nonce, challenge: f.code_challenge, redirect: f.redirect_uri });
    const back = new URL(f.redirect_uri);
    back.searchParams.set("code", code);
    back.searchParams.set("state", f.state);
    res.writeHead(303, { Location: back.toString() });
    return res.end();
  }
  if (url.pathname === "/token" && req.method === "POST") {
    const f = await form(req);
    const grant = codes.get(f.code);
    codes.delete(f.code);
    const verifier = createHash("sha256").update(f.code_verifier ?? "").digest("base64url");
    if (!grant || verifier !== grant.challenge || f.client_id !== CLIENT_ID || f.client_secret !== CLIENT_SECRET || f.redirect_uri !== grant.redirect) {
      return send(res, 400, { error: "invalid_grant" });
    }
    const now = Math.floor(Date.now() / 1000);
    const claims = {
      iss: ISSUER, aud: CLIENT_ID, sub: `fake-${grant.email}`, email: grant.email, email_verified: true,
      name: grant.email.split("@")[0], nonce: grant.nonce, iat: now, exp: now + 300,
    };
    return send(res, 200, { access_token: "unused", token_type: "Bearer", id_token: idToken(claims) });
  }
  send(res, 404, "Not found", "text/plain");
}).listen(PORT, "0.0.0.0", () => console.log(`fake OpenID provider on ${PORT}`));
