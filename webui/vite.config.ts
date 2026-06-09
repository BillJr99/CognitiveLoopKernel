/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The built bundle is emitted INTO the Python package so setuptools ships
// it (see pyproject.toml package-data). FastAPI serves it via
// clk_harness/static_spa.py.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/",
  build: {
    outDir: "../clk_harness/webui_dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    proxy: {
      // `npm run dev` talks to a locally running `clk web` / `clk-api`.
      "/api": { target: "http://127.0.0.1:8001", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
