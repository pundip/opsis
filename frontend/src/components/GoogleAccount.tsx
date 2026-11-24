// A Google Sheets connection's own Google account (OAuth, installed-app style):
// connect, reconnect, paste the address Google comes back to, use a token.json,
// and pick a spreadsheet. The refresh token never reaches the browser.
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  type AuthState,
  type Connection,
  useGoogleFinish,
  useGoogleStart,
  useGoogleToken,
  useSaveConnection,
  useSpreadsheets,
} from "../lib/data";
import { formatDateTime } from "../lib/format";
import { Badge, Button, Card, ErrorNote, Field, Input, PageHeader } from "./ui";

const TESTING_NOTE =
  "If your OAuth app's publishing status in Google Cloud is Testing, Google ends its refresh tokens after 7 days, " +
  "so you would reconnect every week. Setting the app to In production stops that.";

/** The connection's Google account, in one line. */
export function GoogleStatus({ auth }: { auth: AuthState | null | undefined }) {
  if (!auth || auth.status === "not_connected") return <Badge>Not connected</Badge>;
  if (auth.status === "needs_reconnect") return <Badge tone="bad">Needs reconnecting</Badge>;
  return (
    <span className="text-xs">
      <Badge tone="good">Connected</Badge> {auth.account ?? "a Google account"}
    </span>
  );
}

function SpreadsheetPicker({ connection }: { connection: Connection }) {
  const [search, setSearch] = useState("");
  const [asked, setAsked] = useState("");
  const list = useSpreadsheets(connection.id, asked, true);
  const save = useSaveConnection(connection.id);
  const current = String(connection.config.spreadsheet ?? "");
  return (
    <div className="mt-4 border-t border-zinc-200 pt-3 dark:border-zinc-800">
      <h3 className="mb-2 text-sm font-medium">Choose a spreadsheet</h3>
      <form
        className="mb-2 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setAsked(search.trim());
        }}
      >
        <Input
          aria-label="Search your spreadsheets"
          placeholder="Part of its name"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Button type="submit">Search</Button>
      </form>
      <ErrorNote error={list.error ?? save.error} />
      {list.data && list.data.spreadsheets.length === 0 && (
        <p className="text-sm text-zinc-500">No spreadsheets found{asked ? ` for "${asked}"` : ""}.</p>
      )}
      <ul aria-label="Your spreadsheets" className="max-h-64 divide-y divide-zinc-100 overflow-auto dark:divide-zinc-800">
        {(list.data?.spreadsheets ?? []).map((sheet) => {
          const chosen = current.includes(sheet.id);
          return (
            <li key={sheet.id} className="flex items-center gap-2 py-1.5 text-sm">
              <span className="mr-auto">
                {sheet.name}
                <span className="block text-xs text-zinc-500">
                  {[sheet.owner, sheet.modifiedTime ? `changed ${formatDateTime(sheet.modifiedTime)}` : null]
                    .filter(Boolean)
                    .join(", ")}
                </span>
              </span>
              {chosen ? (
                <Badge tone="good">In use</Badge>
              ) : (
                <Button
                  variant="ghost"
                  aria-label={`Use ${sheet.name}`}
                  busy={save.isPending && save.variables?.config.spreadsheet === sheet.id}
                  onClick={() =>
                    save.mutate({
                      name: connection.name,
                      connectorId: connection.connectorId,
                      config: { ...connection.config, spreadsheet: sheet.id },
                    })
                  }
                >
                  Use
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Connect, reconnect or check a connection's Google account. */
export function GooglePanel({ connection, onClose }: { connection: Connection; onClose: () => void }) {
  const start = useGoogleStart();
  const finish = useGoogleFinish();
  const token = useGoogleToken(connection.id);
  const [pasted, setPasted] = useState("");
  const auth = connection.auth;
  const connected = auth?.status === "connected";
  const paste = start.data?.mode === "paste";

  return (
    <Card className="mb-5 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="mr-auto font-medium">Google account for {connection.name}</h2>
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      <p className="mb-2 text-sm">
        <GoogleStatus auth={auth} />
        {connected && auth?.connectedAt && (
          <span className="ml-2 text-xs text-zinc-500">since {formatDateTime(auth.connectedAt)}</span>
        )}
      </p>
      {auth?.status === "needs_reconnect" && (
        <p role="alert" className="mb-2 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-300">
          {auth.error} Scheduled refreshes of its datasets are paused until you reconnect.
        </p>
      )}
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Google asks you to allow read-only access: to see your Google Sheets spreadsheets, and to see the names of your
        Google Drive files, so Opsis can list the spreadsheets you can pick. Opsis keeps only a refresh token, encrypted,
        which nobody can see.
      </p>
      <ErrorNote error={start.error ?? finish.error ?? token.error} />
      {finish.data && (
        <p role="status" className="mb-2 text-sm text-emerald-700 dark:text-emerald-400">
          Connected{finish.data.auth.account ? ` as ${finish.data.auth.account}` : ""}.
        </p>
      )}
      {!paste && (
        <Button
          variant="primary"
          busy={start.isPending}
          onClick={() =>
            start.mutate(connection.id, {
              onSuccess: (started) => {
                // Direct: Google brings this browser back to Opsis, which finishes on its own.
                if (started.mode === "direct") window.location.assign(started.authUrl);
                else window.open(started.authUrl, "_blank", "noopener");
              },
            })
          }
        >
          {auth && auth.status !== "not_connected" ? "Reconnect" : "Connect Google account"}
        </Button>
      )}
      {paste && start.data && (
        <form
          className="mt-1 space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            finish.mutate(pasted.trim(), { onSuccess: () => start.reset() });
          }}
        >
          <p className="text-sm">
            Google opened in a new tab (
            <a href={start.data.authUrl} target="_blank" rel="noopener noreferrer" className="text-brand underline">
              open it again
            </a>
            ). After you allow access, Google sends that tab to an address that does not load: it starts with{" "}
            <code className="break-all">{start.data.redirectUri}</code>. Copy the whole address from that tab's address
            bar and paste it here.
          </p>
          <Field label="The address from the address bar" htmlFor={`google-${connection.id}`}>
            <textarea
              id={`google-${connection.id}`}
              rows={3}
              required
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              className="w-full rounded-md border border-zinc-300 bg-white p-2 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-900"
            />
          </Field>
          <div className="flex gap-2">
            <Button type="submit" variant="primary" busy={finish.isPending}>
              Finish connecting
            </Button>
            <Button variant="ghost" onClick={() => start.reset()}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      <p className="mt-3 text-xs text-zinc-500">{TESTING_NOTE}</p>
      <p className="mt-2 text-xs">
        <label htmlFor={`token-${connection.id}`} className="cursor-pointer text-brand underline">
          Use an existing token.json instead
        </label>{" "}
        <span className="text-zinc-500">
          (written by a Python script with the same OAuth client; it must allow reading spreadsheets).
        </span>
        <input
          id={`token-${connection.id}`}
          type="file"
          accept=".json"
          className="sr-only"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file && file.size < 64 * 1024) void file.text().then((text) => token.mutate(text));
          }}
        />
      </p>
      {token.data && (
        <p role="status" className="mt-1 text-sm text-emerald-700 dark:text-emerald-400">
          Connected from token.json{token.data.auth.account ? ` as ${token.data.auth.account}` : ""}.
        </p>
      )}
      {connected && auth?.canList && <SpreadsheetPicker connection={connection} />}
      {connected && auth && !auth.canList && (
        <p className="mt-3 text-sm text-zinc-600 dark:text-zinc-400">
          This sign-in may not list your spreadsheets. Edit the connection and paste a spreadsheet's URL instead.
        </p>
      )}
    </Card>
  );
}

/** Where Google sends the browser back to, when that is Opsis itself (local mode). */
export function GoogleReturn() {
  const finish = useGoogleFinish();
  const navigate = useNavigate();
  const sent = useRef(false);
  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    const address = window.location.href;
    // The code is single-use, but keep it out of the history all the same.
    window.history.replaceState(null, "", window.location.pathname);
    finish.mutate(address, {
      onSuccess: (done) => navigate(`/connections?google=${done.connectionId}`, { replace: true }),
    });
  }, [finish, navigate]);
  return (
    <div className="mx-auto max-w-xl">
      <PageHeader title="Connecting your Google account" />
      {!finish.isError && <p className="text-sm text-zinc-600 dark:text-zinc-400">One moment...</p>}
      <ErrorNote error={finish.error} />
      {finish.isError && (
        <Link to="/connections" className="text-sm text-brand underline">
          Back to Connections
        </Link>
      )}
    </div>
  );
}
