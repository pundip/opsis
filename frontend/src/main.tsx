import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { App } from "./app/App";
import "./index.css";
import { runtime } from "./lib/runtime";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

// A data router, which asking before unsaved work is left needs (useBlocker). It has
// one route for everything: App keeps its own <Routes>.
const router = createBrowserRouter([{ path: "*", element: <App /> }], {
  basename: runtime.basePath || undefined,
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
