// @vitest-environment node
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { build } from "vite";
import { afterEach, describe, expect, it } from "vitest";
import { productionDependencyBoundary } from "../scripts/production-boundary";

const directories: string[] = [];
afterEach(async () => {
  await Promise.all(directories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function compileDependency(path: string, lazy = false) {
  const repository = await mkdtemp(join(tmpdir(), "mr-lister-build-boundary-"));
  directories.push(repository);
  const entry = join(repository, "web/src/main.ts");
  const intermediate = join(repository, "web/src/screen.ts");
  const dependency = join(repository, path);
  await mkdir(dirname(entry), { recursive: true });
  await mkdir(dirname(dependency), { recursive: true });
  await writeFile(dependency, path.endsWith(".json") ? '{"marker":"example"}' : 'export default "example";');
  let importPath = relative(dirname(intermediate), dependency).replaceAll("\\", "/");
  if (!importPath.startsWith(".")) importPath = `./${importPath}`;
  await writeFile(intermediate, lazy
    ? `export const show = () => import(${JSON.stringify(importPath)});`
    : `import marker from ${JSON.stringify(importPath)}; export const show = () => marker;`);
  await writeFile(entry, 'import { show } from "./screen"; console.log(show());');
  return build({
    configFile: false,
    root: join(repository, "web"),
    logLevel: "silent",
    plugins: [productionDependencyBoundary()],
    build: { write: false, minify: false, rollupOptions: { input: entry } },
  });
}

describe("production dependency boundary", () => {
  it("allows application modules, including real product mockups", async () => {
    await expect(compileDependency("web/src/mockup-preview.ts")).resolves.toBeDefined();
  });

  it.each([
    "web/offline/phase7/activation.ts",
    "web/tests/example.ts",
    "web/scripts/example.ts",
    "tools/legacy/example.ts",
    "contracts/browser/phase6.5.fixtures.json",
    "web/src/example.test.ts",
  ])("rejects a transitive development dependency: %s", async (path) => {
    await expect(compileDependency(path)).rejects.toThrow("Production build imports development-only code");
  });

  it("also rejects development code behind a lazy import", async () => {
    await expect(compileDependency("web/offline/phase7/activation.ts", true))
      .rejects.toThrow("Production build imports development-only code");
  });
});
