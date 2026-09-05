import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { webcrypto } from "node:crypto";
import { afterEach } from "vitest";

if (globalThis.crypto.subtle === undefined) {
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
}

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:preview" });
Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => undefined });

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});
