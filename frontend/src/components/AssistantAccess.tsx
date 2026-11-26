// Admins: who may use the workspace's Claude key (off for everyone until allowed, per
// person or per group), and whether people may bring their own API keys. Admins see
// which providers someone has keys for, never the keys.
import { useState } from "react";
import { type AssistantAccess as Access, useAssistantAccess, useSetByoKeys, useSetGrant } from "../lib/assistant";
import { Badge, Card, Empty, ErrorNote, ROW, TD, TH } from "./ui";

/** A checkbox saved at once: it shows the click while the save runs, and goes back
 * if the save fails (the error is shown above). */
function SavedCheckbox({ checked, save, label, disabled }: { checked: boolean; save: (value: boolean) => Promise<unknown>; label?: string; disabled?: boolean }) {
  const [shown, setShown] = useState<boolean | null>(null);
  return (
    <input
      type="checkbox"
      aria-label={label}
      checked={shown ?? checked}
      disabled={disabled}
      onChange={(event) => {
        const value = event.target.checked;
        setShown(value);
        save(value)
          .catch(() => undefined)
          .finally(() => setShown(null));
      }}
    />
  );
}

function keysOf(keys: Access["people"][number]["keys"]): string {
  const parts = [
    keys.anthropic ? `Anthropic ${keys.anthropic}` : null,
    keys.openai ? `OpenAI ${keys.openai}` : null,
  ].filter(Boolean);
  return parts.join(", ");
}

export function AssistantAccess() {
  const access = useAssistantAccess();
  const byo = useSetByoKeys();
  const grant = useSetGrant();
  const data = access.data;
  return (
    <>
      <ErrorNote error={access.error ?? byo.error ?? grant.error} />
      {data && (
        <>
          <Card className="mb-5 p-4">
            <h2 className="mb-2 font-medium">Personal API keys</h2>
            <label className="flex items-center gap-2 text-sm">
              <SavedCheckbox checked={data.byoKeys} disabled={byo.isPending} save={(value) => byo.mutateAsync(value)} />
              People may use their own Anthropic and OpenAI API keys in the assistant
            </label>
            <p className="mt-2 text-xs text-zinc-500">
              Switched off, keys people already added are kept but not used. Deactivating someone deletes their keys.
            </p>
          </Card>
          <Card className="mb-5 p-4">
            <h2 className="mb-1 font-medium">The workspace's Claude key</h2>
            <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
              {data.workspaceKey.configured
                ? `ANTHROPIC_API_KEY is set (model ${data.workspaceKey.model}). Nobody may use it until you allow them, here or through a group. A change takes effect at once, even for a request that is running.`
                : "There is no workspace key (ANTHROPIC_API_KEY is not set), so what you allow here takes effect once one is set."}
              {data.ollama.enabled && ` Ollama (${data.ollama.model}) is available to everyone.`}
            </p>
            {data.groups.length > 0 && (
              <fieldset className="mb-4">
                <legend className="mb-1 text-sm font-medium">Groups</legend>
                <div className="flex flex-wrap gap-4">
                  {data.groups.map((group) => (
                    <label key={group.id} className="flex items-center gap-1.5 text-sm">
                      <SavedCheckbox
                        label={`Group ${group.name} may use the workspace key`}
                        checked={group.grant === true}
                        disabled={grant.isPending}
                        save={(value) => grant.mutateAsync({ grantee: `group:${group.id}`, allowed: value || null })}
                      />
                      {group.name}
                      {group.grant === false && <Badge tone="bad">Withheld</Badge>}
                    </label>
                  ))}
                </div>
              </fieldset>
            )}
            {data.people.length === 0 ? (
              <Empty>No people yet.</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full" aria-label="Who may use the workspace key">
                  <thead>
                    <tr>
                      <th className={TH}>Person</th>
                      <th className={TH}>Their own keys</th>
                      <th className={TH}>May use the workspace key</th>
                      <th className={TH}>Now</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.people.map((person) => (
                      <tr key={person.id} className={ROW}>
                        <td className={TD}>
                          <span className="font-medium">{person.displayName}</span>
                          <span className="block text-xs text-zinc-500">{person.email}</span>
                        </td>
                        <td className={TD}>{keysOf(person.keys) || <span className="text-zinc-400">None</span>}</td>
                        <td className={TD}>
                          <SavedCheckbox
                            label={`${person.email} may use the workspace key`}
                            checked={person.grant === true}
                            disabled={grant.isPending || !person.isActive}
                            save={(value) => grant.mutateAsync({ grantee: `user:${person.id}`, allowed: value || null })}
                          />
                          {person.grant === false && (
                            <span className="ml-2">
                              <Badge tone="bad">Withheld</Badge>
                            </span>
                          )}
                        </td>
                        <td className={TD}>
                          {person.workspaceKey ? (
                            <Badge tone="good">{person.grant === true ? "Yes" : "Yes, through a group"}</Badge>
                          ) : (
                            <Badge>No</Badge>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  );
}
