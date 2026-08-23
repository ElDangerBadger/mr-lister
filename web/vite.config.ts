import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
