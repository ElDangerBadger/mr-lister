import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { productionDependencyBoundary } from "./scripts/production-boundary";

export default defineConfig({
  plugins: [react(), productionDependencyBoundary()],
  build: {
    target: "es2022",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "https://seller.example.com/" } },
    setupFiles: "./tests/setup.ts",
    css: true,
    restoreMocks: true,
  },
});
