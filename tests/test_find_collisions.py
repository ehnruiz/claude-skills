import contextlib
import hashlib
import importlib.util
import io
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "find_collisions", ROOT / "skills/skill-collision-audit/scripts/find_collisions.py"
)
find_collisions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(find_collisions)

# The fixture library is written to a temporary folder, not committed, so a skill installer that
# scans the repository for SKILL.md never mistakes a fixture for a real skill.
FIXTURES = {
    'a/content-plan/SKILL.md': '---\nname: content-plan\ndescription: Plan social media posts and content themes for the month. Use when the user asks what to post, wants a content calendar, or needs post ideas for social media.\n---\n',
    'a/invoice-chase/SKILL.md': '---\nname: invoice-chase\ndescription: Find overdue invoices in the accounting system and draft polite payment reminders for each client. Use when someone mentions unpaid bills, late payers or accounts receivable.\n---\n',
    'a/social-calendar/SKILL.md': '---\nname: social-calendar\ndescription: >\n  Build a monthly social media content calendar with post ideas.\n  Use when the user asks what to post this month on social media.\n---\n',
    'b/content-plan/SKILL.md': '---\nname: content-plan\ndescription: An older copy of the content planning skill, installed in a second place.\n---\n',
}
_TMP = tempfile.TemporaryDirectory()
LIBRARY = pathlib.Path(_TMP.name) / "library"
for _rel, _text in FIXTURES.items():
    (LIBRARY / _rel).parent.mkdir(parents=True, exist_ok=True)
    (LIBRARY / _rel).write_text(_text, encoding="utf-8")


# Pairs judged with the skill's step 3 and found resolved: each description draws the line.
# Pointing two descriptions at each other raises their shared-word score, so a resolved pair
# can sit over the threshold. Each entry keeps a hash of both descriptions, so editing either
# one reopens the review, and the requests it was judged with, mapped to the skill each should reach.
REVIEWED = {
    frozenset({"explain-the-number", "regression-proof"}): {
        "hash": "f41f0357b23c",
        "requests": {
            "did the tests pass?": "regression-proof",
            "can I say the change improved the Lighthouse score?": "explain-the-number",
        },
    },
}


def description_hash(skills, pair):
    by_name = {s["name"]: s["description"] for s in skills}
    text = "\n".join(f"{name}\n{by_name[name]}" for name in sorted(pair))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def stale_reviews(skills, reviewed):
    """Reviewed pairs whose skills are gone or whose descriptions changed since the review."""
    names = {s["name"] for s in skills}
    problems = []
    for pair, review in reviewed.items():
        a, b = sorted(pair)
        if not pair <= names:
            problems.append(f"{a} <-> {b}: a skill no longer exists; remove the entry")
            continue
        current = description_hash(skills, pair)
        if current != review["hash"]:
            problems.append(
                f"{a} <-> {b}: a description changed since the review; judge the pair again "
                f"with the skill's step 3, then set its hash to {current}"
            )
    return problems


def run(*args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return find_collisions.main([str(a) for a in args])


class FindCollisionsTest(unittest.TestCase):
    def setUp(self):
        self.skills = find_collisions.load_skills([LIBRARY / "a", LIBRARY / "b"])
        self.pairs = find_collisions.find_pairs(self.skills, 0.25)

    def names(self, pair):
        return {pair["a"]["name"], pair["b"]["name"]}

    def test_folded_yaml_description_is_read_in_full(self):
        calendar = next(s for s in self.skills if s["name"] == "social-calendar")
        self.assertIn("what to post this month", calendar["description"])

    def test_two_skills_for_the_same_request_are_flagged(self):
        self.assertIn({"content-plan", "social-calendar"}, [self.names(p) for p in self.pairs])
        pair = next(p for p in self.pairs if self.names(p) == {"content-plan", "social-calendar"})
        self.assertIn("social", pair["shared"])

    def test_an_unrelated_skill_is_not_flagged(self):
        for pair in self.pairs:
            self.assertNotIn("invoice-chase", self.names(pair))

    def test_a_skill_is_never_paired_with_its_own_duplicate(self):
        for pair in self.pairs:
            self.assertNotEqual(pair["a"]["name"], pair["b"]["name"])

    def test_the_same_name_in_two_places_is_a_duplicate(self):
        duplicates = find_collisions.find_duplicate_names(self.skills)
        self.assertEqual(list(duplicates), ["content-plan"])
        self.assertEqual(len(duplicates["content-plan"]), 2)

    def test_common_words_do_not_create_a_collision(self):
        skills = [
            {"name": "x", "path": "x", "description": "Use when the user asks to resize an image for the web."},
            {"name": "y", "path": "y", "description": "Use when the user asks to rotate database credentials."},
        ]
        self.assertEqual(find_collisions.find_pairs(skills, 0.25), [])

    def test_a_skill_invoked_only_by_name_cannot_collide(self):
        text = "Plan social media posts and a monthly content calendar of post ideas."
        skills = [
            {"name": "x", "path": "x", "description": text},
            {"name": "y", "path": "y", "description": text, "manual": True},
        ]
        self.assertEqual(find_collisions.find_pairs(skills, 0.25), [])
        skills[1]["manual"] = False
        self.assertEqual(len(find_collisions.find_pairs(skills, 0.25)), 1)

    def test_exit_codes(self):
        self.assertEqual(run(LIBRARY / "a"), 1)
        self.assertEqual(run(LIBRARY / "a" / "invoice-chase"), 0)
        self.assertEqual(run(LIBRARY / "missing"), 2)

    def test_every_overlapping_pair_in_this_repository_has_been_reviewed(self):
        # A new pair fails here until someone judges it, fixes the descriptions or adds it to REVIEWED.
        skills = find_collisions.load_skills([ROOT / "skills"])
        found = {frozenset({p["a"]["name"], p["b"]["name"]}) for p in find_collisions.find_pairs(skills, 0.25)}
        self.assertEqual(found - set(REVIEWED), set(), "unreviewed overlapping skills; run the skill-collision-audit skill")
        self.assertEqual(find_collisions.find_duplicate_names(skills), {})

    def test_no_reviewed_pair_has_changed_since_its_review(self):
        # Deleting the line one description draws lowers the shared-word score, so the pair can drop
        # under the threshold and leave the test above green. The hash catches that edit.
        skills = find_collisions.load_skills([ROOT / "skills"])
        self.assertEqual(stale_reviews(skills, REVIEWED), [])

    def test_each_reviewed_request_reaches_a_skill_of_its_pair(self):
        for pair, review in REVIEWED.items():
            for request, skill in review["requests"].items():
                self.assertIn(skill, pair, request)

    def test_an_edited_or_missing_description_reopens_a_review(self):
        skills = [
            {"name": "x", "path": "x", "description": "Plan this month's posts. For a single post, use y."},
            {"name": "y", "path": "y", "description": "Write one post. For a month of posts, use x."},
        ]
        pair = frozenset({"x", "y"})
        reviewed = {pair: {"hash": description_hash(skills, pair), "requests": {}}}
        self.assertEqual(stale_reviews(skills, reviewed), [])
        skills[1]["description"] = "Write one post."
        self.assertIn("description changed", stale_reviews(skills, reviewed)[0])
        self.assertIn("no longer exists", stale_reviews(skills[:1], reviewed)[0])

if __name__ == "__main__":
    unittest.main()
