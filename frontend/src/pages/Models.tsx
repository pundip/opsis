import { Link } from "react-router-dom";
import { useModels } from "../lib/semantic";
import { timeAgo } from "../lib/format";
import { Badge, Card, Empty, ErrorNote, PageHeader, ROW, TD, TH } from "../components/ui";

export function Models() {
  const models = useModels();
  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Semantic models"
        description="Each dataset has a model that names its dimensions and measures once, so every dashboard and the assistant use the same definitions."
      />
      <ErrorNote error={models.error} />
      <Card>
        {models.data?.length === 0 ? (
          <Empty>
            No datasets yet.{" "}
            <Link to="/datasets" className="text-brand underline">
              Create a dataset
            </Link>{" "}
            and its model appears here.
          </Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Dataset</th>
                <th className={`${TH} text-right`}>Dimensions</th>
                <th className={`${TH} text-right`}>Measures</th>
                <th className={TH}>Joins</th>
                <th className={TH}>Row-level security</th>
                <th className={TH}>Status</th>
              </tr>
            </thead>
            <tbody>
              {models.data?.map((model) => (
                <tr key={model.dataset} className={ROW}>
                  <td className={TD}>
                    <Link to={`/models/${model.dataset}`} className="font-medium text-brand underline">
                      {model.label}
                    </Link>
                    <span className="block font-mono text-xs text-zinc-500">{model.dataset}</span>
                  </td>
                  <td className={`${TD} text-right tabular-nums`}>{model.dimensions}</td>
                  <td className={`${TD} text-right tabular-nums`}>{model.measures}</td>
                  <td className={TD}>{model.joins.join(", ")}</td>
                  <td className={TD}>
                    {model.rlsRules > 0
                      ? `${model.rlsRules} rule${model.rlsRules === 1 ? "" : "s"}`
                      : "None"}
                  </td>
                  <td className={TD}>
                    {!model.hasData ? (
                      <Badge>No data yet</Badge>
                    ) : model.isStarter ? (
                      <Badge>Generated</Badge>
                    ) : (
                      <>
                        <Badge tone="good">Edited</Badge>{" "}
                        {model.updatedAt && (
                          <span className="text-xs text-zinc-500">{timeAgo(model.updatedAt)}</span>
                        )}
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
