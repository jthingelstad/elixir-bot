import { readFile } from "node:fs/promises";
import { glob } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

const files = [];

for await (const file of glob("**/*.json", {
  cwd: repoRoot,
  exclude: ["tools/docs-build/**"]
})) {
  files.push(file);
}

let failures = 0;

for (const file of files.sort()) {
  try {
    JSON.parse(await readFile(resolve(repoRoot, file), "utf8"));
  } catch (error) {
    failures += 1;
    console.error(`${file}: invalid JSON`);
    console.error(error.message);
  }
}

if (failures > 0) {
  process.exit(1);
}

console.log(`Validated ${files.length} JSON file(s).`);
