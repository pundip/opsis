// The user guide: pages the api renders from docs/guide, and the links in them.
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./api";

export interface GuidePage {
  page: string;
  title: string;
  html: string;
}

/** One page of the guide; "" is the contents page. */
export function useGuidePage(page: string) {
  return useQuery({
    queryKey: ["guide", page],
    queryFn: () => apiGet<GuidePage>(page ? `/api/guide/${encodeURIComponent(page)}` : "/api/guide"),
    staleTime: Infinity, // it changes only with the app
    retry: false,
  });
}

/** The route for a link to a page or heading of the guide, or null for any other link.
 * The api writes these links with the base path, which the router adds itself. */
export function guideRoute(href: string, basePath: string): string | null {
  if (!href.startsWith(`${basePath}/`)) return null;
  const route = href.slice(basePath.length);
  return /^\/docs\/guide($|[/#?])/.test(route) ? route : null;
}

/** The id of the heading that an address ending in "#..." points at, or null. */
export function headingId(hash: string): string | null {
  const anchor = hash.replace(/^#/, "");
  if (!anchor) return null;
  try {
    return `guide-${decodeURIComponent(anchor)}`; // the api prefixes heading ids
  } catch {
    return null; // a badly escaped address points nowhere
  }
}
