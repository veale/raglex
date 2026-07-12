import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API calls to the FastAPI backend so the UI and API can be
// developed on separate ports without CORS friction; in production the built
// static files are served behind the same origin (or any static host).
// RAGLEX_DEV_API overrides the target; RAGLEX_DEV_API_PREFIX=1 keeps the /api
// prefix (for pointing dev at a full deployment, which mounts the API there).
const target = process.env.RAGLEX_DEV_API || "http://127.0.0.1:8000";
const keepPrefix = process.env.RAGLEX_DEV_API_PREFIX === "1";
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target, changeOrigin: true, ...(keepPrefix ? {} : { rewrite: (p) => p.replace(/^\/api/, "") }) },
    },
  },
});
