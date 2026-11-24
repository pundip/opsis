import { Link } from "react-router-dom";

export function Placeholder({ title, missing = false }: { title: string; missing?: boolean }) {
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
        {missing
          ? "This page does not exist, or the feature is switched off on this server."
          : "This page is not available."}
      </p>
      <Link to="/" className="mt-4 inline-block text-sm font-medium text-brand underline">
        Back to home
      </Link>
    </div>
  );
}
