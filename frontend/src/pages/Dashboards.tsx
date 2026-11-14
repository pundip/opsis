import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { Me } from "../lib/config";
import { useCreateDashboard, useDashboards, useDeleteDashboard } from "../lib/dashboard";
import { timeAgo } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, Input, PageHeader, ROW, TD, TH } from "../components/ui";

export function Dashboards({ me }: { me: Me }) {
  const dashboards = useDashboards();
  const create = useCreateDashboard();
  const remove = useDeleteDashboard();
  const navigate = useNavigate();
  const [title, setTitle] = useState("");
  const canEdit = me.permissions.includes("dashboards:edit");

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader title="Dashboards" description="Build a dashboard by hand, or ask the assistant to build one. Both edit the same versioned definition." />
      {canEdit && (
        <form
          className="mb-5 flex max-w-xl gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate(title.trim(), { onSuccess: (created) => navigate(`/dashboards/${created.id}/edit`) });
          }}
        >
          <Input
            aria-label="Name of the new dashboard"
            placeholder="Name of the new dashboard"
            value={title}
            required
            maxLength={200}
            onChange={(event) => setTitle(event.target.value)}
          />
          <Button type="submit" variant="primary" busy={create.isPending} className="shrink-0">
            New dashboard
          </Button>
        </form>
      )}
      <ErrorNote error={dashboards.error ?? create.error ?? remove.error} />
      <Card>
        {dashboards.data?.length === 0 ? (
          <Empty>{canEdit ? "No dashboards yet. Name one above to start." : "No dashboards have been published yet."}</Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Dashboard</th>
                <th className={TH}>Status</th>
                <th className={TH}>Owner</th>
                <th className={TH}>Changed</th>
                <th className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {dashboards.data?.map((dashboard) => (
                <tr key={dashboard.id} className={ROW}>
                  <td className={TD}>
                    <Link to={`/dashboards/${dashboard.id}`} className="font-medium text-brand underline">
                      {dashboard.title}
                    </Link>
                  </td>
                  <td className={TD}>
                    {dashboard.publishedVersion === null ? (
                      <Badge>Draft</Badge>
                    ) : dashboard.hasUnpublishedChanges ? (
                      <>
                        <Badge tone="good">Published</Badge> <Badge tone="busy">Unpublished changes</Badge>
                      </>
                    ) : (
                      <Badge tone="good">Published</Badge>
                    )}
                  </td>
                  <td className={TD}>{dashboard.ownerName}</td>
                  <td className={TD}>{timeAgo(dashboard.updatedAt)}</td>
                  <td className={`${TD} text-right whitespace-nowrap`}>
                    {canEdit && (
                      <>
                        <Link to={`/dashboards/${dashboard.id}/edit`} className="mr-3 text-sm text-brand underline">
                          Edit
                        </Link>
                        <Button
                          variant="ghost"
                          onClick={() => {
                            if (window.confirm(`Delete the dashboard "${dashboard.title}" and all its versions?`)) {
                              remove.mutate(dashboard.id);
                            }
                          }}
                        >
                          Delete
                        </Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
