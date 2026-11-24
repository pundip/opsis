// A stand-in mail server for the server-mode smoke tests (compose.servertest.yml).
// It accepts every message over plain SMTP (no TLS, no sign-in) and lists them over
// HTTP as JSON, so a test can read the link in an invitation:
//   SMTP on SMTP_PORT (2525)
//   GET http://localhost:9600/messages?to=<address>  ->  [{to, from, subject, text, html}], oldest first
// Nothing is kept on disk. Test messages only.
import http from "node:http";
import net from "node:net";

const SMTP_PORT = Number(process.env.SMTP_PORT ?? 2525);
const HTTP_PORT = Number(process.env.HTTP_PORT ?? 9600);
const messages = [];

function headersOf(block) {
  const headers = {};
  for (const line of block.replace(/\r?\n[ \t]+/g, " ").split(/\r?\n/)) {
    const at = line.indexOf(":");
    if (at > 0) headers[line.slice(0, at).trim().toLowerCase()] = line.slice(at + 1).trim();
  }
  return headers;
}

function decodeBody(body, encoding) {
  const how = (encoding ?? "7bit").toLowerCase();
  if (how === "base64") return Buffer.from(body.replace(/\s+/g, ""), "base64").toString("utf8");
  if (how === "quoted-printable") {
    const bytes = body.replace(/=\r?\n/g, "").replace(/=([0-9A-Fa-f]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
    return Buffer.from(bytes, "latin1").toString("utf8");
  }
  return body;
}

/** The text and HTML parts of a message, enough of MIME for what Opsis sends. */
function parse(raw) {
  const split = raw.search(/\r?\n\r?\n/);
  const headers = headersOf(raw.slice(0, split));
  const body = raw.slice(split).replace(/^\r?\n\r?\n/, "");
  const found = { text: "", html: "" };
  const visit = (partHeaders, partBody) => {
    const type = (partHeaders["content-type"] ?? "text/plain").toLowerCase();
    const boundary = /boundary="?([^";]+)"?/i.exec(partHeaders["content-type"] ?? "")?.[1];
    if (type.startsWith("multipart/") && boundary) {
      for (const chunk of partBody.split(`--${boundary}`).slice(1)) {
        if (chunk.startsWith("--")) break;
        const at = chunk.search(/\r?\n\r?\n/);
        visit(headersOf(chunk.slice(0, at).replace(/^\r?\n/, "")), chunk.slice(at).replace(/^\r?\n\r?\n/, ""));
      }
      return;
    }
    const decoded = decodeBody(partBody, partHeaders["content-transfer-encoding"]);
    if (type.startsWith("text/html")) found.html ||= decoded;
    else if (type.startsWith("text/plain")) found.text ||= decoded;
  };
  visit(headers, body);
  return { to: headers.to ?? "", from: headers.from ?? "", subject: headers.subject ?? "", ...found };
}

net
  .createServer((socket) => {
    let buffer = "";
    let inData = false;
    const reply = (line) => socket.write(`${line}\r\n`);
    reply("220 fake-smtp ready");
    socket.on("data", (chunk) => {
      buffer += chunk.toString("latin1");
      for (;;) {
        if (inData) {
          const end = buffer.indexOf("\r\n.\r\n");
          if (end < 0) return;
          const raw = buffer.slice(0, end).replace(/\r\n\.\./g, "\r\n.");
          buffer = buffer.slice(end + 5);
          inData = false;
          messages.push(parse(Buffer.from(raw, "latin1").toString("latin1")));
          reply("250 OK: queued");
          continue;
        }
        const end = buffer.indexOf("\r\n");
        if (end < 0) return;
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const verb = line.slice(0, 4).toUpperCase();
        if (verb === "EHLO") socket.write("250-fake-smtp\r\n250 8BITMIME\r\n");
        else if (verb === "HELO") reply("250 fake-smtp");
        else if (verb === "DATA") {
          inData = true;
          reply("354 End data with <CR><LF>.<CR><LF>");
        } else if (verb === "QUIT") {
          reply("221 Bye");
          socket.end();
          return;
        } else reply("250 OK");
      }
    });
    socket.on("error", () => undefined);
  })
  .listen(SMTP_PORT, () => console.log(`fake-smtp: SMTP on ${SMTP_PORT}`));

http
  .createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://localhost");
    if (url.pathname !== "/messages") {
      response.writeHead(404).end();
      return;
    }
    const to = url.searchParams.get("to");
    const found = to ? messages.filter((m) => m.to.toLowerCase().includes(to.toLowerCase())) : messages;
    response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(found));
  })
  .listen(HTTP_PORT, () => console.log(`fake-smtp: messages on http://localhost:${HTTP_PORT}/messages`));
