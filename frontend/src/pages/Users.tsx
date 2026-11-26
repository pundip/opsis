// People and groups (admins, sign-in modes). Invitations and password resets are
// emailed with FEATURE_EMAIL; otherwise, or when the email cannot be sent, they are
// one-time links shown here once, for the admin to pass on.
import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, Field, Input, PageHeader, ROW, Select, TD, TH } from "../components/ui";
import {
  type Delivery,
  type Group,
  type Role,
  type UserRow,
  useDeleteGroup,
  useGroups,
  useInvite,
  useNewPasswordLink,
  useSaveGroup,
  useUpdateUser,
  useUsers,
} from "../lib/access";
import { AssistantAccess } from "../components/AssistantAccess";
import { type Me, useConfig } from "../lib/config";
import { timeAgo } from "../lib/format";

const ROLES: Role[] = ["viewer", "editor", "admin"];
const ROLE_HELP: Record<Role, string> = {
  viewer: "sees published dashboards shared with them",
  editor: "builds datasets, models and dashboards",
  admin: "everything, including people and connections",
};

/** A link shown once, with a copy button. */
export function OneTimeLink({ label, link, note, onDone }: { label: string; link: string; note?: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div role="alert" className="mb-4 rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
      <p className="font-medium">{label}</p>
      {note && <p className="text-xs text-amber-800 dark:text-amber-300">{note}</p>}
      <p className="mb-2 text-xs text-zinc-600 dark:text-zinc-400">It is shown only now. Send it to the person yourself.</p>
      <div className="flex flex-wrap items-center gap-2">
        <code className="break-all rounded bg-white px-2 py-1 font-mono text-xs dark:bg-zinc-900" aria-label="One-time link">
          {link}
        </code>
        <Button onClick={() => void navigator.clipboard.writeText(link).then(() => setCopied(true), () => setCopied(false))}>
          {copied ? "Copied" : "Copy"}
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

/** An emailed link is confirmed; a link that was not emailed is shown once. */
function Delivered({ label, delivery, onDone }: { label: string; delivery: Delivery; onDone: () => void }) {
  if (delivery.link === null) {
    return (
      <div
        role="status"
        className="mb-4 flex items-center gap-2 rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40"
      >
        <p className="mr-auto">{label}</p>
        <Button variant="ghost" onClick={onDone}>
          Done
        </Button>
      </div>
    );
  }
  return (
    <OneTimeLink
      label={label}
      link={delivery.link}
      note={delivery.emailError ? `The email could not be sent: ${delivery.emailError}` : undefined}
      onDone={onDone}
    />
  );
}

function AttributesEditor({ value, onChange }: { value: Record<string, unknown>; onChange: (next: Record<string, unknown> | null) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 1));
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <Field
      label="Attributes for row-level security"
      htmlFor="attributes"
      hint='JSON, for example {"regions": ["VIC", "NSW"]}. Rules in the semantic models use these.'
      error={problem ?? undefined}
    >
      <textarea
        id="attributes"
        className="h-20 w-full rounded-md border border-zinc-300 bg-white p-2 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-950"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          try {
            const parsed: unknown = JSON.parse(event.target.value || "{}");
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
            setProblem(null);
            onChange(parsed as Record<string, unknown>);
          } catch {
            setProblem("This is not a JSON object yet.");
            onChange(null);
          }
        }}
      />
    </Field>
  );
}

function UserEditor({ user, groups, onClose }: { user: UserRow; groups: Group[]; onClose: () => void }) {
  const update = useUpdateUser();
  const [attributes, setAttributes] = useState<Record<string, unknown> | null>(user.attributes);
  const [groupIds, setGroupIds] = useState(user.groups.map((g) => g.id));
  return (
    <div className="space-y-3 rounded-md bg-zinc-50 p-3 dark:bg-zinc-950">
      <AttributesEditor value={user.attributes} onChange={setAttributes} />
      {groups.length > 0 && (
        <fieldset>
          <legend className="mb-1 text-sm font-medium">Groups</legend>
          <div className="flex flex-wrap gap-3">
            {groups.map((group) => (
              <label key={group.id} className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={groupIds.includes(group.id)}
                  onChange={() => setGroupIds((now) => (now.includes(group.id) ? now.filter((g) => g !== group.id) : [...now, group.id]))}
                />
                {group.name}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <ErrorNote error={update.error} />
      <div className="flex gap-2">
        <Button
          variant="primary"
          disabled={attributes === null}
          busy={update.isPending}
          onClick={() => attributes && update.mutate({ id: user.id, attributes, groupIds }, { onSuccess: onClose })}
        >
          Save
        </Button>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function People({ me, groups }: { me: Me; groups: Group[] }) {
  const users = useUsers();
  const invite = useInvite();
  const update = useUpdateUser();
  const newLink = useNewPasswordLink();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [shown, setShown] = useState<{ label: string; delivery: Delivery } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  return (
    <>
      {shown && <Delivered label={shown.label} delivery={shown.delivery} onDone={() => setShown(null)} />}
      <Card className="mb-5 p-4">
        <h2 className="mb-3 font-medium">Invite someone</h2>
        <form
          className="grid gap-3 sm:grid-cols-[1fr_1fr_10rem_auto] sm:items-end"
          onSubmit={(event) => {
            event.preventDefault();
            invite.mutate(
              { email: email.trim(), displayName: name.trim(), role },
              {
                onSuccess: (made) => {
                  setShown({
                    label: made.emailed
                      ? `Invitation emailed to ${made.user.email}. Its link works for 7 days.`
                      : `Invitation for ${made.user.email} (valid 7 days)`,
                    delivery: made,
                  });
                  setEmail("");
                  setName("");
                },
              },
            );
          }}
        >
          <Field label="Email" htmlFor="invite-email">
            <Input id="invite-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Name" htmlFor="invite-name">
            <Input id="invite-name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Role" htmlFor="invite-role">
            <Select id="invite-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </Select>
          </Field>
          <Button type="submit" variant="primary" busy={invite.isPending}>
            Invite
          </Button>
        </form>
        <p className="mt-2 text-xs text-zinc-500">{ROLES.map((r) => `${r}: ${ROLE_HELP[r]}`).join(". ")}.</p>
        <ErrorNote error={invite.error} />
      </Card>
      <ErrorNote error={users.error ?? update.error ?? newLink.error} />
      <Card>
        {users.data?.length ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className={TH}>Person</th>
                  <th className={TH}>Role</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>Groups</th>
                  <th className={TH}>Last sign-in</th>
                  <th className={TH}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {users.data.map((user) => (
                  <tr key={user.id} className={ROW}>
                    <td className={TD}>
                      <span className="font-medium">{user.displayName}</span>
                      <span className="block text-xs text-zinc-500">{user.email}</span>
                      {editing === user.id && (
                        <div className="mt-2">
                          <UserEditor user={user} groups={groups} onClose={() => setEditing(null)} />
                        </div>
                      )}
                    </td>
                    <td className={TD}>
                      <Select
                        aria-label={`Role of ${user.email}`}
                        className="!w-28"
                        value={user.role}
                        onChange={(e) => update.mutate({ id: user.id, role: e.target.value as Role })}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className={TD}>
                      <Badge tone={user.status === "active" ? "good" : user.status === "invited" ? "busy" : "bad"}>{user.status}</Badge>
                      {user.google && <span className="ml-1 text-xs text-zinc-500">Google</span>}
                    </td>
                    <td className={TD}>{user.groups.map((g) => g.name).join(", ") || <span className="text-zinc-400">None</span>}</td>
                    <td className={TD}>{user.lastLoginAt ? timeAgo(user.lastLoginAt) : "Never"}</td>
                    <td className={`${TD} whitespace-nowrap text-right`}>
                      <Button variant="ghost" onClick={() => setEditing(editing === user.id ? null : user.id)}>
                        Edit
                      </Button>
                      {user.isActive || user.status === "invited" ? (
                        <Button
                          variant="ghost"
                          onClick={() =>
                            newLink.mutate(user.id, {
                              onSuccess: (delivery) =>
                                setShown({
                                  label:
                                    user.status === "invited"
                                      ? delivery.emailed
                                        ? `A new invitation was emailed to ${user.email}.`
                                        : `New invitation for ${user.email}`
                                      : delivery.emailed
                                        ? `A password reset link was emailed to ${user.email} (valid 24 hours).`
                                        : `Password reset for ${user.email} (valid 24 hours)`,
                                  delivery,
                                }),
                            })
                          }
                        >
                          {user.status === "invited" ? "New invitation" : "Reset link"}
                        </Button>
                      ) : null}
                      {user.id !== me.id && (
                        <Button
                          variant={user.isActive ? "danger" : "secondary"}
                          aria-label={`${user.isActive ? "Deactivate" : "Reactivate"} ${user.email}`}
                          title={user.isActive ? "Signs them out everywhere and deletes their own AI provider keys." : undefined}
                          onClick={() => update.mutate({ id: user.id, isActive: !user.isActive })}
                        >
                          {user.isActive ? "Deactivate" : "Reactivate"}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No people yet.</Empty>
        )}
      </Card>
    </>
  );
}

function Groups({ groups, users }: { groups: Group[]; users: UserRow[] }) {
  const save = useSaveGroup();
  const remove = useDeleteGroup();
  const [name, setName] = useState("");
  return (
    <>
      <Card className="mb-5 p-4">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate({ name: name.trim() }, { onSuccess: () => setName("") });
          }}
        >
          <Field label="New group" htmlFor="group-name">
            <Input id="group-name" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Button type="submit" variant="primary" busy={save.isPending}>
            Create group
          </Button>
        </form>
        <ErrorNote error={save.error ?? remove.error} />
      </Card>
      {groups.length === 0 ? (
        <Empty>No groups yet. Groups make sharing with several people easier.</Empty>
      ) : (
        groups.map((group) => {
          const members = group.members.map((m) => m.id);
          return (
            <Card key={group.id} className="mb-3 p-4">
              <div className="mb-2 flex items-center gap-2">
                <h3 className="mr-auto font-medium">{group.name}</h3>
                <Button variant="danger" aria-label={`Delete group ${group.name}`} onClick={() => remove.mutate(group.id)}>
                  Delete
                </Button>
              </div>
              <div className="flex flex-wrap gap-3">
                {users
                  .filter((u) => u.isActive)
                  .map((user) => (
                    <label key={user.id} className="flex items-center gap-1.5 text-sm">
                      <input
                        type="checkbox"
                        checked={members.includes(user.id)}
                        onChange={() =>
                          save.mutate({
                            id: group.id,
                            name: group.name,
                            description: group.description,
                            memberIds: members.includes(user.id) ? members.filter((m) => m !== user.id) : [...members, user.id],
                          })
                        }
                      />
                      {user.displayName}
                    </label>
                  ))}
              </div>
            </Card>
          );
        })
      )}
    </>
  );
}

const TABS = { people: "People", groups: "Groups", assistant: "AI assistant" } as const;

export function Users({ me }: { me: Me }) {
  const [tab, setTab] = useState<keyof typeof TABS>("people");
  const groups = useGroups();
  const users = useUsers();
  const config = useConfig();
  const tabs = (Object.keys(TABS) as (keyof typeof TABS)[]).filter((key) => key !== "assistant" || config.data?.features.assistant);
  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="Users" description="Who can sign in, what they may do, and the groups you share with." />
      <div role="tablist" className="mb-4 flex gap-1">
        {tabs.map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`rounded-md px-3 py-1.5 text-sm ${tab === key ? "bg-zinc-200/70 font-medium dark:bg-zinc-800" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400"}`}
          >
            {TABS[key]}
          </button>
        ))}
      </div>
      <ErrorNote error={groups.error} />
      {tab === "people" && <People me={me} groups={groups.data ?? []} />}
      {tab === "groups" && <Groups groups={groups.data ?? []} users={users.data ?? []} />}
      {tab === "assistant" && <AssistantAccess />}
    </div>
  );
}
