import { realpathSync } from "node:fs";
import { relative, resolve } from "node:path";
import type { Plugin } from "vite";

// Inspect resolved dependencies, including lazy imports, before tree shaking can hide one.
export function productionDependencyBoundary(): Plugin {
  let repositoryRoot = "";
  return {
    name: "mr-lister-production-dependency-boundary",
    apply: "build",
    configResolved(config) {
      repositoryRoot = resolve(realpathSync(config.root), "..");
    },
    moduleParsed(module) {
      if (module.id.startsWith("\0")) return;
      const filename = module.id.split("?")[0];
      if (filename === undefined) return;
      const path = relative(repositoryRoot, filename).split("\\").join("/");
      const developmentDirectory = /^(?:web\/(?:offline|scripts|tests|__tests__|__mocks__|fixtures)|tools|tests|docs|docs_legacy)\//u;
      const fixtureFile = /^(?:web|contracts)\/.*(?:^|[./_-])(?:fixtures?|mock|demo)(?:[./_-]|$)/u;
      const testFile = /^web\/.*\.(?:test|spec)\.[^/]+$/u;
      const testDependency = /(?:^|\/)node_modules\/(?:vitest|@vitest|@testing-library|msw)(?:\/|$)/u;
      if (developmentDirectory.test(path) || fixtureFile.test(path)
        || testFile.test(path) || testDependency.test(path)) {
        this.error(`Production build imports development-only code: ${path}`);
      }
    },
  };
}
