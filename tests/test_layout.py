import json
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
LINK = re.compile(r"\]\(([^)\s]+)\)")
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "hooks/README.md", *sorted((ROOT / "docs").glob("*.md"))]


def frontmatter(path):
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def relative_links(text):
    """The file paths that Markdown links in `text` point at: no URLs, no bare anchors, fragments stripped."""
    for target in LINK.findall(text):
        path = target.split("#", 1)[0]
        if path and ":" not in path:
            yield path


def broken_links(path):
    return [target for target in relative_links(path.read_text(encoding="utf-8")) if not (path.parent / target).exists()]


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
            self.assertEqual(broken_links(path), [], path)

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
        self.assertGreater(len(DOCS), 3)
        for path in DOCS:
            self.assertEqual(broken_links(path), [], path)

    def test_link_check_strips_fragments_and_skips_urls_and_anchors(self):
        text = "[a](x.md#section) [b](https://example.com/y.md) [c](#top) [d](../z.md)"
        self.assertEqual(list(relative_links(text)), ["x.md", "../z.md"])

    def test_link_check_reports_a_missing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            page = pathlib.Path(folder) / "page.md"
            (pathlib.Path(folder) / "here.md").write_text("", encoding="utf-8")
            page.write_text("[ok](here.md#part) [gone](nowhere.md#part)", encoding="utf-8")
            self.assertEqual(broken_links(page), ["nowhere.md"])

    def test_install_commands_name_the_plugin_as_the_manifests_do(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        owner_repo = plugin["repository"].removeprefix("https://github.com/")
        for path in (ROOT / "README.md", ROOT / "docs/install.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"/plugin marketplace add {owner_repo}", text, path)
            self.assertIn(f"/plugin install {plugin['name']}@{market['name']}", text, path)

    def test_changelog_has_a_section_for_the_current_version(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = re.compile(rf"^## {re.escape(plugin['version'])} \(", re.M)
        self.assertTrue(heading.search(changelog), f"CHANGELOG.md has no section for {plugin['version']}")


if __name__ == "__main__":
    unittest.main()
