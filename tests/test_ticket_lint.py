import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ticket_lint", ROOT / "skills/ticket-write/scripts/ticket_lint.py"
)
ticket_lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_lint)

SHORT_BUG = """cc @Sam

**What happens**
Product pages lose their search data when a description contains a link.

**Why it matters**
Those pages drop out of rich results.

**Done when**
- A product with a link in its description shows valid search data
- Other product pages are unchanged

**Links**
Related: https://example.com/browse/WEB-1
"""


def verdicts(text, kind="bug", title=None):
    rows, identifiers = ticket_lint.lint(text, kind, title)
    return {name: ok for name, _, _, ok in rows}, identifiers


class TicketLintTest(unittest.TestCase):
    def test_a_short_plain_bug_passes(self):
        result, identifiers = verdicts(SHORT_BUG, title="Product pages lose search data")
        self.assertTrue(all(result.values()), result)
        self.assertEqual(identifiers, [])

    def test_cc_line_headings_and_links_are_not_counted_as_words(self):
        words = ticket_lint.count_words(SHORT_BUG.splitlines())
        self.assertEqual(words, 36)

    def test_code_identifiers_are_found_and_over_the_limit(self):
        text = SHORT_BUG.replace(
            "contains a link.",
            "contains a link: `sanitize()` in libs/shared/product.tsx calls buildJsonLd with raw_html.",
        )
        result, identifiers = verdicts(text)
        self.assertFalse(result["code identifiers"])
        self.assertIn("libs/shared/product.tsx", identifiers)

    def test_plain_english_has_no_identifiers(self):
        _, identifiers = verdicts("Visitors on iPhone can't open the PDF. It's slow on Wi-Fi.")
        self.assertEqual(identifiers, [])

    def test_a_long_title_fails(self):
        result, _ = verdicts(SHORT_BUG, title="x" * 81)
        self.assertFalse(result["title characters"])

    def test_spike_budget_is_tighter(self):
        body = "**Question**\n" + "word " * 100 + "\n\n**Done when**\n- one\n- two\n"
        self.assertFalse(verdicts(body, "spike")[0]["description words"])
        self.assertTrue(verdicts(body, "bug")[0]["description words"])

    def test_criteria_outside_done_when_are_not_counted(self):
        body = "**Goal**\n- a\n- b\n- c\n\n**Done when**\n- only one\n"
        self.assertFalse(verdicts(body, "task")[0]["acceptance criteria"])

    def test_cli_exit_codes(self):
        draft = ROOT / "tests" / "_draft.md"
        draft.write_text(SHORT_BUG, encoding="utf-8")
        try:
            self.assertEqual(ticket_lint.main(["--type", "bug", str(draft)]), 0)
            self.assertEqual(ticket_lint.main(["--type", "bug", "--title", "x" * 90, str(draft)]), 1)
            self.assertEqual(ticket_lint.main(["--type", "bug", str(draft) + ".missing"]), 2)
        finally:
            draft.unlink()


if __name__ == "__main__":
    unittest.main()
