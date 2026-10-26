import contextlib
import importlib.util
import json
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


# Pairs judged with the skill's step 3, saved by the audit beside the library it ran on. Each entry
# keeps a hash of both descriptions, so editing either one reopens the review, and the requests it
# was judged with, which `replay_routing.py` sends to Claude to check where they land.
REVIEWED_FILE = ROOT / "skills/reviewed-pairs.json"


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

    def test_shared_words_with_equal_weight_are_listed_in_order(self):
        # A set's order changes with the hash seed, so ties are broken by the word itself.
        text = "zeta theta eta epsilon delta gamma beta alpha"
        skills = [{"name": "x", "path": "x", "description": text}, {"name": "y", "path": "y", "description": text}]
        shared = find_collisions.find_pairs(skills, 0.25)[0]["shared"]
        self.assertEqual(shared, ["alpha", "beta", "delta", "epsilon", "eta", "gamma"])

    def test_exit_codes(self):
        self.assertEqual(run(LIBRARY / "a"), 1)
        self.assertEqual(run(LIBRARY / "a" / "invoice-chase"), 0)
        self.assertEqual(run(LIBRARY / "missing"), 2)

    def test_every_overlapping_pair_in_this_repository_has_been_reviewed(self):
        # A new pair fails here until someone judges it, fixes the descriptions or adds it to the file.
        skills = find_collisions.load_skills([ROOT / "skills"])
        reviewed = find_collisions.load_reviewed(REVIEWED_FILE)
        found = {frozenset({p["a"]["name"], p["b"]["name"]}) for p in find_collisions.find_pairs(skills, 0.25)}
        self.assertEqual(found - set(reviewed), set(), "unreviewed overlapping skills; run the skill-collision-audit skill")
        self.assertEqual(find_collisions.find_duplicate_names(skills), {})

    def test_every_review_in_this_repository_is_current_and_complete(self):
        # Deleting the line one description draws lowers the shared-word score, so the pair can drop
        # under the threshold and leave the test above green. The hash catches that edit.
        skills = find_collisions.load_skills([ROOT / "skills"])
        reviewed = find_collisions.load_reviewed(REVIEWED_FILE)
        self.assertEqual(find_collisions.review_problems(skills, reviewed), [])

    def test_an_edited_or_missing_description_reopens_a_review(self):
        skills = self.two_skills()
        reviewed = self.review(skills)
        self.assertEqual(find_collisions.review_problems(skills, reviewed), [])
        skills[1]["description"] = "Write one post."
        self.assertIn("description changed", find_collisions.review_problems(skills, reviewed)[0])
        self.assertIn("no longer exists", find_collisions.review_problems(skills[:1], reviewed)[0])
        reviewed[frozenset({"x", "y"})]["hash"] = ""
        self.assertIn("not hashed yet; set its hash to", find_collisions.review_problems(skills, reviewed)[0])

    def test_a_review_needs_three_requests_for_its_pair_and_one_for_neither(self):
        skills = self.two_skills()
        cases = {
            "fewer than three": [("plan the month", "x"), ("write one post", "y"), ("book a flight", None)],
            "neither skill": [("plan the month", "x"), ("plan next month", "x"), ("write one post", "y")],
            "no request reaches": [("plan the month", "x"), ("plan next month", "x"), ("plan May", "x"), ("book a flight", None)],
            "outside the pair": [("plan the month", "x"), ("plan next month", "x"), ("write one post", "y"), ("book a flight", "z"), ("rename a file", None)],
        }
        for message, requests in cases.items():
            with self.subTest(message):
                problems = find_collisions.review_problems(skills, self.review(skills, requests))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(message, problems[0])

    def test_the_reviewed_file_round_trips_and_rejects_a_bad_entry(self):
        skills = self.two_skills()
        path = pathlib.Path(_TMP.name) / "reviewed-pairs.json"
        entry = {"skills": ["y", "x"], "hash": "abc", "requests": [{"request": "plan the month", "expect": "x"}, {"request": "book a flight", "expect": None}]}
        path.write_text(json.dumps({"pairs": [entry]}), encoding="utf-8")
        reviewed = find_collisions.load_reviewed(path)
        self.assertEqual(reviewed[frozenset({"x", "y"})]["requests"], [("plan the month", "x"), ("book a flight", None)])
        bad_files = [
            {"pairs": [{**entry, "skills": ["x"]}]},
            {"pairs": [{**entry, "skills": [["x"], "y"]}]},
            {"pairs": [{**entry, "skills": ["x", "x"]}]},
            {"pairs": [entry, entry]},
            {"pairs": [{**entry, "requests": [{"request": "", "expect": "x"}]}]},
            {"pairs": [{**entry, "requests": None}]},
            {"pairs": [{**entry, "requests": ["plan the month"]}]},
            {"pairs": [{**entry, "requests": {"plan the month": "x"}}]},
            {"pairs": [{**entry, "hash": 123}]},
            [],
        ]
        for bad in bad_files:
            with self.subTest(bad=bad):
                path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    find_collisions.load_reviewed(path)
        with self.assertRaises(ValueError):
            find_collisions.load_reviewed(pathlib.Path(_TMP.name))
        self.assertEqual(find_collisions.load_reviewed(pathlib.Path(_TMP.name) / "absent.json"), {})

    def test_a_current_review_clears_its_pair_and_a_stale_one_flags_it(self):
        library = pathlib.Path(_TMP.name) / "cli"
        for rel in ("a/content-plan/SKILL.md", "a/social-calendar/SKILL.md"):
            (library / rel).parent.mkdir(parents=True, exist_ok=True)
            (library / rel).write_text(FIXTURES[rel], encoding="utf-8")
        skills = find_collisions.load_skills([library])
        pair = ["content-plan", "social-calendar"]
        requests = [{"request": "plan May's posts", "expect": "content-plan"}, {"request": "what should I post?", "expect": "content-plan"},
                    {"request": "a calendar of posts for June", "expect": "social-calendar"}, {"request": "chase an unpaid invoice", "expect": None}]
        reviewed_file = library / "reviewed-pairs.json"
        for digest, code in ((find_collisions.description_hash(skills, frozenset(pair)), 0), ("000000000000", 1)):
            with self.subTest(code=code):
                reviewed_file.write_text(json.dumps({"pairs": [{"skills": pair, "hash": digest, "requests": requests}]}), encoding="utf-8")
                self.assertEqual(run("--reviewed", reviewed_file, library), code)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    find_collisions.main(["--json", "--reviewed", str(reviewed_file), str(library)])
                report = json.loads(out.getvalue())
                self.assertEqual([p["reviewed"] for p in report["pairs"]], [True])
                self.assertEqual(len(report["review_problems"]), code)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            find_collisions.main(["--reviewed", str(reviewed_file), str(library)])
        self.assertIn("0 overlapping pairs, 1 reviewed", out.getvalue())
        self.assertIn("social-calendar  (reviewed)", out.getvalue())
        self.assertEqual(run("--reviewed", library, library), 2)

    def two_skills(self):
        return [
            {"name": "x", "path": "x", "description": "Plan this month's posts. For a single post, use y."},
            {"name": "y", "path": "y", "description": "Write one post. For a month of posts, use x."},
        ]

    def review(self, skills, requests=(("plan the month", "x"), ("plan next month", "x"), ("write one post", "y"), ("book a flight", None))):
        pair = frozenset({"x", "y"})
        return {pair: {"hash": find_collisions.description_hash(skills, pair), "requests": list(requests)}}


if __name__ == "__main__":
    unittest.main()
