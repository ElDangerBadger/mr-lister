import { readdir } from "node:fs/promises";
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

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const paths = await Promise.all(entries.map(async (entry) => {
    const absolute = new URL(entry.name + (entry.isDirectory() ? "/" : ""), directory);
    if (entry.isDirectory()) return walk(absolute);
    return [relative(new URL("../dist/", import.meta.url).pathname, absolute.pathname)];
  }));
  return paths.flat();
}
