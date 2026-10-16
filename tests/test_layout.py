import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def frontmatter(path):
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


class LayoutTest(unittest.TestCase):
    def test_every_skill_names_its_folder_and_has_a_description(self):
        skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for path in skills:
            fields = frontmatter(path)
            self.assertEqual(fields.get("name"), path.parent.name, path)
            self.assertGreater(len(fields.get("description", "")), 40, path)

    def test_every_agent_has_a_name_description_and_model(self):
        for path in sorted((ROOT / "agents").glob("*.md")):
            fields = frontmatter(path)
            self.assertEqual(fields.get("name"), path.stem, path)
            self.assertIn(fields.get("model"), {"haiku", "sonnet", "opus"}, path)
            self.assertTrue(fields.get("description"), path)

    def test_relative_links_in_skills_resolve(self):
        for path in (ROOT / "skills").rglob("*.md"):
            for target in re.findall(r"\]\(([^)#:]+)\)", path.read_text(encoding="utf-8")):
                self.assertTrue((path.parent / target).exists(), f"{path}: {target}")

    def test_readme_lists_every_skill_and_agent(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            self.assertIn(f"`{path.parent.name}`", readme)
        for path in (ROOT / "agents").glob("*.md"):
            self.assertIn(f"`{path.stem}`", readme)

    def test_manifests_agree(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual([p["name"] for p in market["plugins"]], [plugin["name"]])

    def test_relative_links_in_docs_resolve(self):
        docs = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "hooks/README.md", *sorted((ROOT / "docs").glob("*.md"))]
        self.assertGreater(len(docs), 3)
        for path in docs:
            for target in re.findall(r"\]\(([^)#:]+)\)", path.read_text(encoding="utf-8")):
                self.assertTrue((path.parent / target).exists(), f"{path}: {target}")

    def test_readme_install_command_names_the_plugin_as_the_manifests_do(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"/plugin marketplace add ehnruiz/claude-skills", readme)
        self.assertIn(f"/plugin install {plugin['name']}@{market['name']}", readme)

    def test_changelog_has_a_section_for_the_current_version(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = re.compile(rf"^## {re.escape(plugin['version'])} \(", re.M)
        self.assertTrue(heading.search(changelog), f"CHANGELOG.md has no section for {plugin['version']}")


if __name__ == "__main__":
    unittest.main()
