import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Where `npm run dev` sends /api. Overridable so the UI can be developed
// against an API on another port or another machine; in production the nginx
// sidecar does this instead.
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
