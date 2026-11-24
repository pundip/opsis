// A stand-in for Google, for the smoke tests' Google Sheets connection: the
// consent page (in the browser), the token endpoint (PKCE checked), tokeninfo,
// and the bits of the Drive and Sheets APIs the connector reads. Test data only.
//   GET  /o/oauth2/auth      consent page with Allow and Cancel
//   POST /token              authorization_code and refresh_token grants
//   GET  /tokeninfo          the scopes of an access token
//   POST /revoke-all         test helper: every refresh token stops working (invalid_grant)
//   GET  /drive/v3/about, /drive/v3/files, /v4/spreadsheets/...
import { createHash, randomBytes } from "node:crypto";
import http from "node:http";

const PORT = Number(process.env.PORT ?? 9700);
const CLIENT_ID = process.env.CLIENT_ID ?? "smoke-client.apps.googleusercontent.com";
const CLIENT_SECRET = process.env.CLIENT_SECRET ?? "smoke-secret";
const ACCOUNT = "smoke@gmail.test";
const SHEET = "1SmokeSheetSmokeSheetSmokeSheet0123";
const SCOPES = {
  read: "https://www.googleapis.com/auth/spreadsheets.readonly",
  list: "https://www.googleapis.com/auth/drive.metadata.readonly",
};
const codes = new Map();
const refreshTokens = new Map();
const accessTokens = new Map();
const random = () => randomBytes(18).toString("base64url");
const escape = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// One spreadsheet with a "Gifts" tab. 46037 is 15 January 2026 as a Sheets serial number.
const VALUES = [
  ["fund", "amount", "gift_date"],
  ["Library", 100, 46037],
  ["Sport", 250.5, 46038],
  ["Research", 75, 46040],
];
const cell = (value, type) =>
  typeof value === "number"
    ? { effectiveValue: { numberValue: value }, ...(type ? { effectiveFormat: { numberFormat: { type } } } : {}) }
    : { effectiveValue: { stringValue: value } };
const GRID = VALUES.map((row, i) => row.map((v, j) => cell(v, i > 0 && j === 2 ? "DATE" : undefined)));

function send(res, status, body, type = "application/json") {
  const data = type === "application/json" ? JSON.stringify(body) : body;
  res.writeHead(status, { "Content-Type": type });
  res.end(data);
}

function issueAccess(scopes) {
  const token = `ya29.${random()}`;
  accessTokens.set(token, { scopes });
  return { access_token: token, expires_in: 3599, scope: scopes.join(" "), token_type: "Bearer" };
}

function consentPage(query) {
  const scopes = (query.get("scope") ?? "").split(" ").filter(Boolean);
  const hidden = [...query.entries()]
    .map(([k, v]) => `<input type="hidden" name="${escape(k)}" value="${escape(v)}">`)
    .join("");
  return `<!doctype html><html><head><title>Sign in - Google Accounts (fake)</title></head><body>
<h1>Opsis smoke wants to access your Google Account</h1><p>${ACCOUNT}</p>
<ul>${scopes.map((s) => `<li>${escape(s)}</li>`).join("")}</ul>
<form method="get" action="/approve">${hidden}<button name="answer" value="allow">Allow</button>
<button name="answer" value="cancel">Cancel</button></form></body></html>`;
}

function tokenGrant(form, basic) {
  let clientId = form.get("client_id");
  let clientSecret = form.get("client_secret");
  if (basic) [clientId, clientSecret] = Buffer.from(basic, "base64").toString().split(":");
  if (clientId !== CLIENT_ID || clientSecret !== CLIENT_SECRET) return [401, { error: "invalid_client" }];
  if (form.get("grant_type") === "authorization_code") {
    const grant = codes.get(form.get("code"));
    codes.delete(form.get("code"));
    const challenge = createHash("sha256").update(form.get("code_verifier") ?? "").digest("base64url");
    if (!grant || grant.redirectUri !== form.get("redirect_uri") || grant.challenge !== challenge) {
      return [400, { error: "invalid_grant", error_description: "Bad Request" }];
    }
    const refresh = `1//${random()}`;
    refreshTokens.set(refresh, { scopes: grant.scopes, revoked: false });
    return [200, { ...issueAccess(grant.scopes), refresh_token: refresh }];
  }
  if (form.get("grant_type") === "refresh_token") {
    const known = refreshTokens.get(form.get("refresh_token"));
    if (!known || known.revoked) return [400, { error: "invalid_grant", error_description: "Token has been expired or revoked." }];
    return [200, issueAccess(known.scopes)];
  }
  return [400, { error: "unsupported_grant_type" }];
}

function api(url, res, bearer) {
  const grant = accessTokens.get(bearer);
  if (!grant) return send(res, 401, { error: { code: 401, message: "Invalid Credentials" } });
  const path = decodeURIComponent(url.pathname);
  if (path === "/drive/v3/about") return send(res, 200, { user: { emailAddress: ACCOUNT, displayName: "Smoke" } });
  if (path === "/drive/v3/files") {
    return send(res, 200, {
      files: [{ id: SHEET, name: "Smoke giving", modifiedTime: "2026-11-20T01:00:00Z", owners: [{ emailAddress: ACCOUNT }] }],
    });
  }
  if (path === `/v4/spreadsheets/${SHEET}` && url.searchParams.get("includeGridData")) {
    return send(res, 200, { sheets: [{ data: [{ rowData: GRID.map((values) => ({ values })) }] }] });
  }
  if (path === `/v4/spreadsheets/${SHEET}`) {
    return send(res, 200, {
      properties: { title: "Smoke giving" },
      sheets: [{ properties: { sheetId: 0, title: "Gifts", sheetType: "GRID", gridProperties: { rowCount: VALUES.length, columnCount: 3 } } }],
    });
  }
  if (path.startsWith(`/v4/spreadsheets/${SHEET}/values/`)) return send(res, 200, { values: VALUES });
  return send(res, 404, { error: { code: 404, message: "Requested entity was not found." } });
}

http
  .createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://fake-google");
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      if (req.method === "GET" && url.pathname === "/o/oauth2/auth") {
        return send(res, 200, consentPage(url.searchParams), "text/html");
      }
      if (req.method === "GET" && url.pathname === "/approve") {
        const q = url.searchParams;
        const back = new URL(q.get("redirect_uri"));
        back.searchParams.set("state", q.get("state") ?? "");
        if (q.get("answer") !== "allow") {
          back.searchParams.set("error", "access_denied");
        } else {
          const code = random();
          const scopes = (q.get("scope") ?? "").split(" ").filter(Boolean);
          codes.set(code, { challenge: q.get("code_challenge"), redirectUri: q.get("redirect_uri"), scopes });
          back.searchParams.set("code", code);
          back.searchParams.set("scope", scopes.join(" "));
        }
        res.writeHead(302, { Location: back.toString() });
        return res.end();
      }
      if (req.method === "POST" && url.pathname === "/token") {
        const auth = req.headers.authorization ?? "";
        const [status, answer] = tokenGrant(new URLSearchParams(body), auth.startsWith("Basic ") ? auth.slice(6) : null);
        return send(res, status, answer);
      }
      if (req.method === "GET" && url.pathname === "/tokeninfo") {
        const grant = accessTokens.get(url.searchParams.get("access_token"));
        return grant ? send(res, 200, { aud: CLIENT_ID, scope: grant.scopes.join(" ") }) : send(res, 400, { error: "invalid_token" });
      }
      if (req.method === "POST" && url.pathname === "/revoke-all") {
        for (const token of refreshTokens.values()) token.revoked = true;
        return send(res, 200, { revoked: refreshTokens.size });
      }
      if (req.method === "GET") return api(url, res, (req.headers.authorization ?? "").replace(/^Bearer /, ""));
      return send(res, 404, {});
    });
  })
  .listen(PORT, () => console.log(`fake-google on ${PORT}; scopes ${Object.values(SCOPES).join(", ")}`));
