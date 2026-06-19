import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { glob } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

const markdownFiles = [];

for await (const file of glob("**/*.{md,txt}", {
  cwd: repoRoot,
  exclude: ["tools/docs-build/**"]
})) {
  markdownFiles.push(file);
}

const markdownLinkPattern = /(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
const failures = [];

for (const file of markdownFiles.sort()) {
  const text = await readFile(resolve(repoRoot, file), "utf8");

  for (const match of text.matchAll(markdownLinkPattern)) {
    const rawHref = match[1];
    const hrefWithoutAnchor = rawHref.split("#", 1)[0];

    if (
      hrefWithoutAnchor === "" ||
      /^[a-z][a-z0-9+.-]*:/i.test(hrefWithoutAnchor)
    ) {
      continue;
    }

    if (hrefWithoutAnchor.startsWith("/")) {
      failures.push(`${file}: absolute internal link is not portable: ${rawHref}`);
      continue;
    }

    const target = resolve(repoRoot, dirname(file), decodeURIComponent(hrefWithoutAnchor));
    if (!existsSync(target)) {
      failures.push(`${file}: missing internal link target: ${rawHref}`);
    }
  }
}

if (failures.length > 0) {
  for (const failure of failures) {
    console.error(failure);
  }
  process.exit(1);
}

console.log(`Validated links in ${markdownFiles.length} markdown/text file(s).`);
