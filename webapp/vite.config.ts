/// <reference types="vitest" />
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
  test: {
    // `node`, not a fake DOM: what is worth testing here is the model — pure
    // functions over the draft — and a test that renders a component to find
    // out what `setKeys` did would be testing React as well as the thing it
    // meant to. The screen is checked in a browser instead (§8.5).
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
