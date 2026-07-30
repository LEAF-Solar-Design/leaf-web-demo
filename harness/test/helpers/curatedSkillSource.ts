import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

type CurationEntry = {
  name: string;
};

export function createCuratedSkillSource(root: string, curationPath: string): string {
  const source = join(root, "skill-source");
  const entries = JSON.parse(readFileSync(curationPath, "utf8")) as CurationEntry[];

  mkdirSync(source);
  for (const { name } of entries) {
    const skillDirectory = join(source, name);
    mkdirSync(skillDirectory);
    const description = name === "code-standards"
      ? [
          "description: >-",
          "  Apply a disciplined engineering workflow to code changes,",
          "  with focused tests and verification.",
        ].join("\n")
      : `description: Hermetic test fixture for the ${name} curated skill.`;
    writeFileSync(
      join(skillDirectory, "SKILL.md"),
      ["---", `name: ${name}`, description, "---", "Use this fixture only in automated tests.", ""].join("\n"),
    );
  }

  return source;
}
