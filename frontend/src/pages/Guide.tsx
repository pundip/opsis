// The user guide inside the app, at {base}/docs/guide. The api renders each page
// from the Markdown in docs/guide (opsis/services/user_guide.py): this shows it.
import { useEffect, useMemo, type MouseEvent } from "react";
import { Link, useLocation, useNavigate, useNavigationType, useParams } from "react-router-dom";
import { Card, Empty, ErrorNote } from "../components/ui";
import { guideRoute, headingId, useGuidePage } from "../lib/guide";
import { runtime } from "../lib/runtime";

export function Guide() {
  const { page = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const navigationType = useNavigationType();
  const guide = useGuidePage(page);
  // The same object for the same page, so React leaves what is on screen alone.
  const content = useMemo(() => ({ __html: guide.data?.html ?? "" }), [guide.data?.html]);
  const shown = guide.data?.page;

  // The text arrives after the address changes, so the browser cannot find the
  // heading itself: go to it (or to the top) once the page is on screen.
  useEffect(() => {
    if (!shown) return;
    // Back, forward and reload: leave the page where the browser put it.
    if (navigationType === "POP" && window.scrollY > 0) return;
    const id = headingId(location.hash);
    const heading = id ? document.getElementById(id) : null;
    if (heading) heading.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [shown, location.key, location.hash, navigationType]);

  // Links within the guide change the page without loading the app again. The
  // rest (new tabs, modified clicks, other sites) is left to the browser.
  function follow(event: MouseEvent<HTMLElement>) {
    const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
    if (event.defaultPrevented || event.button !== 0 || modified) return;
    const link = (event.target as Element).closest("a");
    if (!link || link.target) return;
    const route = guideRoute(link.getAttribute("href") ?? "", runtime.basePath);
    if (route === null) return;
    event.preventDefault();
    void navigate(route);
  }

  if (guide.error) {
    return (
      <div className="mx-auto max-w-4xl space-y-3">
        <ErrorNote error={guide.error} />
        <p className="text-sm">
          <Link to="/docs/guide" className="text-brand underline">
            Guide contents
          </Link>
        </p>
      </div>
    );
  }
  if (!guide.data) return <Empty>Loading</Empty>;
  return (
    <Card className="mx-auto max-w-4xl p-6">
      <article className="guide" onClick={follow} dangerouslySetInnerHTML={content} />
    </Card>
  );
}
