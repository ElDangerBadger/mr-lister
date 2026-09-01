import { readFile, readdir } from "node:fs/promises";
import { relative } from "node:path";
import { URL } from "node:url";

const root = new URL("../dist/", import.meta.url);
const files = await walk(root);
const forbidden = files.filter((file) => (
  file.endsWith(".map")
  || /(?:^|\/)runtime-config(?:\.example)?\.json$/u.test(file)
));

if (forbidden.length > 0) {
  throw new Error(`Production build contains forbidden public artifacts: ${forbidden.join(", ")}`);
}

const disabledPhase7Markers = [
  "/publication",
  "/publish",
  "data-phase7-publication-workspace",
  "publish_exact_approved_listing",
];
const compiledFiles = files.filter((file) => file.endsWith(".html") || file.endsWith(".js"));
const phase7Leaks = [];
for (const file of compiledFiles) {
  const source = await readFile(new URL(file, root), "utf8");
  if (disabledPhase7Markers.some((marker) => source.includes(marker))) phase7Leaks.push(file);
}
if (phase7Leaks.length > 0) {
  throw new Error(`Production build contains disabled Phase 7 capability: ${phase7Leaks.join(", ")}`);
}

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const paths = await Promise.all(entries.map(async (entry) => {
    const absolute = new URL(entry.name + (entry.isDirectory() ? "/" : ""), directory);
    if (entry.isDirectory()) return walk(absolute);
    return [relative(new URL("../dist/", import.meta.url).pathname, absolute.pathname)];
  }));
  return paths.flat();
}
