import contextlib
import importlib.util
import io
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "find_collisions", ROOT / "skills/skill-collision-audit/scripts/find_collisions.py"
)
find_collisions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(find_collisions)

LIBRARY = ROOT / "tests/fixtures/library"


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

    def test_this_repository_has_no_collisions(self):
        self.assertEqual(run(ROOT / "skills"), 0)


if __name__ == "__main__":
    unittest.main()
