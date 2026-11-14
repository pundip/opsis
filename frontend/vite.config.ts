import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In dev mode the API runs in the api container; compose.dev.yml sets VITE_API_PROXY.
const apiTarget = process.env.VITE_API_PROXY ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", sourcemap: false },
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    // Polling keeps hot reload working when the repo is on the Windows filesystem.
    watch: process.env.VITE_USE_POLLING ? { usePolling: true, interval: 300 } : undefined,
    proxy: {
      "/api": apiTarget,
      "/mcp": apiTarget,
      "/healthz": apiTarget,
    },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
