import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audit_runner", ROOT / "skills/security-audit-runner/scripts/audit_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FIXTURES = ROOT / "tests/fixtures/audit_runner"

# A stand-in for Cloudflare's security-audit skill with the same file names, headings and block shapes but none of
# its text. It is written to a temporary folder, not committed, so a skill installer that scans the repository for
# SKILL.md never mistakes it for a real skill. The runner reads it the way it reads the installed skill.
SKILL_FILES = {
    "SKILL.md": "---\nname: security-audit\ndescription: Fixture.\n---\n\n# Fixture\n",
    "ATTACK-CLASSES.md": (
        "# Attack classes\n\n#### Attack classes\n\nPreamble.\n\n"
        "**Access control** (subagent_type: `general`)\n\nAccess control fixture text.\n\n"
        "**Injection** (subagent_type: `general`)\n\nInjection fixture text.\n"
    ),
    "EXAMPLE-COMPANION.md": (
        "# Example companion\n\n#### When to use this file\n\nWhen text.\n\n"
        "## Core discipline (include in every agent prompt for this domain)\n\nCore fixture text.\n\n"
        "## Example attack classes (subagent_type: `general`)\n\n"
        "**Example class**\nExample class text.\n\n**Other class**\nOther class text.\n\n"
        "## Universal moves (apply across the above)\n\nMoves fixture text.\n\n"
        "## Validation rules (apply before reporting ANY finding here)\n\nRules fixture text.\n"
    ),
    "HUNTING.md": (
        "# Hunting\n\n#### Core hunting method\n\n```text\nMETHOD-TEXT\n```\n\n"
        "#### Promotion procedure\n\n```text\nPROMOTION-TEXT\n```\n\n"
        "#### Core validation rules\n\n```text\nGATE-TEXT\n```\n\n"
        "## Structured hunter result\n\nCONTRACT-TEXT\n\n## Parent consolidation and ledger update\n\nParent text.\n\n"
        "## Coverage-critic waves\n\nCRITIC-TEXT\n\nThe parent rejects proposed units outside the scope.\n"
    ),
    "VALIDATION-AND-REPORTING.md": (
        "# Validation\n\n#### Candidate-verifier prompt\n\n```text\nVERIFIER-TEXT\n```\n\n"
        "Copy this promotion procedure verbatim into every candidate-verifier prompt:\n\n```text\nPROMOTION-TEXT\n```\n\n"
        "For `confirmed`, require it to check:\n\n1. CHECKLIST-TEXT\n\n"
        "Each verifier returns exactly one JSON object: REPLY-SHAPE-TEXT\n\n"
        "Do not apply a Phase 5 replacement before it is checked.\n"
    ),
    "RECONNAISSANCE.md": "".join(
        f"**Agent {a}: fixture**\n\n```text\nRECON-{a} for <target>\n```\n\n" for a in ("1a", "1b", "1c", "1d")),
}


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_skill(folder):
    folder.mkdir(parents=True)
    for name, text in SKILL_FILES.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "report-schema.json").write_text((FIXTURES / "report-schema.json").read_text(encoding="utf-8"), encoding="utf-8")


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = runner.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


def write_transcript(path, text, handback=False):
    final = ([{"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}}] if handback
             else [{"type": "text", "text": text}])
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Read the prompt file."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Reading the prompt."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": final}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n", encoding="utf-8")


def as_record(candidate):
    record = {k: v for k, v in candidate.items() if k != "proposed_verdict"}
    record["verdict"] = candidate["proposed_verdict"]
    return record


class TextRulesTest(unittest.TestCase):
    def test_coverage_id_percent_encodes_each_ref_and_joins_them(self):
        refs = {"surface": "src/api.js#GET /items", "boundary": "src/authz.js#requireOwner", "subsystem": "packages/café",
                "attack_class": "ATTACK-CLASSES.md#Access control"}
        self.assertEqual(runner.coverage_id(refs), "src%2Fapi.js%23GET%20%2Fitems::src%2Fauthz.js%23requireOwner"
                                                   "::packages%2Fcaf%C3%A9::ATTACK-CLASSES.md%23Access%20control")

    def test_coverage_id_rejects_padded_unnormalized_or_invisible_refs(self):
        for ref in (" src/a.js", "café", "a​b", "\t"):
            with self.assertRaises(runner.RunnerError, msg=repr(ref)):
                runner.encode_ref(ref)

    def test_safe_path_follows_the_findings_validator(self):
        self.assertTrue(runner.safe_path("src/api.js"))
        for path in ("/etc/passwd", "../x", "a/./b", "a\\b", "src/a.js:12", "con.txt", "dir/name.", "~/x", ""):
            self.assertFalse(runner.safe_path(path), path)


class RecordCheckTest(unittest.TestCase):
    def setUp(self):
        schema = load_fixture("report-schema.json")
        self.branches = {b["properties"]["verdict"]["const"]: b for b in schema["items"]["oneOf"]}

    def test_fixture_records_pass(self):
        self.assertEqual(runner.draft_errors(load_fixture("candidate-needs-validation.json"), self.branches), [])
        self.assertEqual(runner.record_errors(load_fixture("record-confirmed.json"), self.branches), [])

    def test_a_trace_with_two_sinks_is_caught(self):
        candidate = load_fixture("candidate-needs-validation.json")
        candidate["trace"][1]["kind"] = "sink"
        self.assertIn("record.trace[1].kind: must be propagation, got 'sink'", runner.draft_errors(candidate, self.branches))

    def test_overall_severity_cannot_exceed_the_demonstrated_impact(self):
        record = load_fixture("record-confirmed.json")
        record["severity"] = {"impact": {"score": "medium"}, "overall_severity": "critical"}
        self.assertTrue(any("exceeds the demonstrated impact" in e for e in runner.record_errors(record, self.branches)))

    def test_needs_validation_needs_a_plan_and_safe_paths(self):
        record = as_record(load_fixture("candidate-needs-validation.json"))
        record["validation_plan"] = {}
        record["evidence"][0]["file"] = "/abs/src/api.js"
        errors = runner.record_errors(record, self.branches)
        self.assertIn("record.validation_plan: needs a visible local or deployment plan", errors)
        self.assertIn("record.evidence[0].file: must be a safe repository-relative path", errors)

    def test_a_field_from_another_branch_is_refused(self):
        record = as_record(load_fixture("candidate-needs-validation.json"))
        record["severity"] = {"impact": {"score": "low"}, "overall_severity": "low"}
        self.assertIn("record: unexpected field 'severity'", runner.record_errors(record, self.branches))


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = pathlib.Path(folder.name) / "agent.output"

    def test_the_handback_message_is_the_reply(self):
        write_transcript(self.path, '{"a": 1}', handback=True)
        self.assertEqual(runner.final_message(self.path), '{"a": 1}')

    def test_otherwise_the_last_assistant_text_is_the_reply(self):
        write_transcript(self.path, '{"b": 2}')
        self.assertEqual(runner.final_message(self.path), '{"b": 2}')


class SkillBlockTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        write_skill(pathlib.Path(folder.name) / "skill")
        self.skill = runner.Skill(pathlib.Path(folder.name) / "skill")

    def test_a_class_block_ends_at_the_next_class(self):
        block = self.skill.block("ATTACK-CLASSES.md#Access control")
        self.assertIn("Access control fixture text.", block)
        self.assertNotIn("Injection", block)
        self.assertEqual(self.skill.block("EXAMPLE-COMPANION.md#Example class"), "**Example class**\nExample class text.")

    def test_a_companion_section_runs_to_the_next_heading(self):
        block = self.skill.block("EXAMPLE-COMPANION.md#Core discipline")
        self.assertTrue(block.startswith("## Core discipline (include"))
        self.assertNotIn("Example class", block)

    def test_a_missing_anchor_names_the_tested_version(self):
        with self.assertRaises(runner.RunnerError) as caught:
            self.skill.fenced_after("HUNTING.md", "#### Renamed heading")
        self.assertIn(runner.TESTED_COMMIT, str(caught.exception))

    def test_a_block_ref_outside_the_skill_is_refused(self):
        for ref in ("../SECRET.md#x", "ATTACK-CLASSES.md", "notes.md#x"):
            with self.assertRaises(runner.RunnerError):
                self.skill.block(ref)


class RunCase(unittest.TestCase):
    profile = "quick"
    init_args = ()
    validator_args = ("--without-validators",)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = pathlib.Path(folder.name).resolve()
        write_skill(self.tmp / "skill")
        (self.tmp / "target/src").mkdir(parents=True)
        (self.tmp / "target/src/api.js").write_text("export function getItem(req) {}\n", encoding="utf-8")
        self.run_dir = self.tmp / "run"
        code, _, err = cli("init", self.run_dir, "--target", self.tmp / "target", "--profile", self.profile,
                           "--skill-dir", self.tmp / "skill", "--repo", "example/app", *self.validator_args, *self.init_args)
        self.assertEqual(code, 0, err)
        (self.run_dir / "architecture.md").write_text("Example service with one items route.\n", encoding="utf-8")
        self.seed(self.specs())

    def specs(self):
        units = [
            {"surface": "GET /items", "boundary": "Item owner check", "attack_class": "Access control",
             "refs": {"surface": "src/api.js#getItem", "boundary": "src/api.js#ownerCheck"}, "starting_paths": ["src/api.js"],
             "ordinary_block": "ATTACK-CLASSES.md#Access control", "companions": {"EXAMPLE-COMPANION.md": ["Example class"]},
             "priority": 1},
            {"surface": "Search", "boundary": "Query builder", "attack_class": "Injection",
             "refs": {"surface": "src/search.js#search", "boundary": "src/search.js#buildQuery"},
             "starting_paths": ["src/search.js"], "ordinary_block": "ATTACK-CLASSES.md#Injection",
             "excluded_blocks": [{"block": "EXAMPLE-COMPANION.md#Other class", "reason": "No such path in source."}],
             "priority": 2},
        ]
        if self.profile != "quick":
            for unit in units:
                unit["subsystem"] = "API"
                unit["refs"]["subsystem"] = "src"
        return units

    def seed(self, specs):
        path = self.tmp / f"spec-{len(list(self.tmp.glob('spec-*')))}.json"
        path.write_text(json.dumps(specs), encoding="utf-8")
        code, _, err = cli("seed", self.run_dir, path)
        self.assertEqual(code, 0, err)

    def read(self, name):
        return json.loads((self.run_dir / name).read_text(encoding="utf-8"))

    def unit(self, surface):
        return next(u for u in self.read("coverage-ledger.json") if u["surface"] == surface)

    def call(self, agent_id):
        return next(c for c in self.read("agent-calls.json")["calls"] if c["agent_id"] == agent_id)

    def reply(self, agent_id, obj, handback=False):
        path = self.tmp / f"{agent_id}.output"
        write_transcript(path, obj if isinstance(obj, str) else json.dumps(obj), handback)
        return cli("extract", self.run_dir, agent_id, path)

    def hunter_reply(self, cid, agent_id, candidate=None, disposition=None, **unit_fields):
        unit = {"coverage_id": cid, "disposition": disposition or ("candidate" if candidate else "covered"), "reviewed_paths": ["src/api.js"],
                "checks": [{"agent_id": agent_id, "reviewed_paths": ["src/api.js"], "invariant": "The caller owns the item.",
                            "method": "source", "result": "No owner comparison before the response.", "artifact": None}],
                "candidate_fingerprints": [candidate["fingerprint"]] if candidate else [], "unresolved": []}
        unit.update(unit_fields)
        return {"units": [unit], "candidates": [candidate] if candidate else [], "hardening": ["Validate the id format."],
                "uncovered": []}

    def hunt(self, agent_id, surface, candidate=None, **unit_fields):
        cid = self.unit(surface)["coverage_id"]
        self.assertEqual(cli("assign", self.run_dir, agent_id, cid)[0], 0)
        code, _, err = cli("prompt", "hunter", self.run_dir, agent_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.reply(agent_id, self.hunter_reply(cid, agent_id, candidate, **unit_fields), handback=True)[0], 0)
        return cli("apply-hunter", self.run_dir, agent_id)

    def critic(self, agent_id, reply=None, *apply_args, final_clean=False):
        code, _, err = cli("prompt", "critic", self.run_dir, agent_id, *(["--final-clean"] if final_clean else []))
        self.assertEqual(code, 0, err)
        reply = reply or {"missing_units": [], "reassign_ids": [], "resolved_prior_leads": [], "stop": True}
        self.assertEqual(self.reply(agent_id, reply)[0], 0)
        code, out, err = cli("apply-critic", self.run_dir, agent_id, *apply_args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def verify(self, agent_id, fp, reply):
        code, _, err = cli("prompt", "verifier", self.run_dir, agent_id, "--fingerprint", fp)
        self.assertEqual(code, 0, err)
        extracted = self.reply(agent_id, reply)
        return extracted, cli("apply-verifier", self.run_dir, agent_id)


class QuickRunTest(RunCase):
    def test_the_hunter_prompt_carries_every_part_in_the_contract_order(self):
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("assign", self.run_dir, "h-search", self.unit("Search")["coverage_id"])
        code, out, err = cli("prompt", "hunter", self.run_dir, "h-items")
        self.assertEqual(code, 0, err)
        text = (self.run_dir / "agents/h-items/prompt.md").read_text(encoding="utf-8")
        self.assertIn("agents/h-items/prompt.md", json.loads(out)["agent_prompt"])
        order = ["using source review only", "Example service", cid, "Access control fixture text.", "Core fixture text.",
                 "Example class text.", "Moves fixture text.", "Rules fixture text.", "METHOD-TEXT", "PROMOTION-TEXT",
                 "GATE-TEXT", self.unit("Search")["coverage_id"], "must not run target-controlled code", "CONTRACT-TEXT",
                 '"const": "confirmed"', '"const": "needs_validation"']
        positions = [text.find(marker) for marker in order]
        self.assertNotIn(-1, positions, dict(zip(order, positions)))
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Other class text.", text)
        self.assertNotIn('"const": "rejected"', text)

    def test_a_hunter_reply_closes_its_unit_and_links_its_candidate(self):
        candidate = load_fixture("candidate-needs-validation.json")
        code, out, err = self.hunt("h-items", "GET /items", candidate)
        self.assertEqual(code, 0, out)
        unit = self.unit("GET /items")
        self.assertEqual((unit["status"], unit["agent_id"]), ("candidate", "h-items"))
        self.assertEqual(unit["result_fingerprints"], [candidate["fingerprint"]])
        self.assertEqual(unit["hardening_notes"], ["Validate the id format."])
        self.assertEqual(self.read("candidates.json")[candidate["fingerprint"]]["proposals"][0]["hunter"], "h-items")

    def test_a_candidate_with_two_sinks_is_dropped_and_its_unit_replanned(self):
        candidate = load_fixture("candidate-needs-validation.json")
        candidate["trace"][1]["kind"] = "sink"
        code, out, _ = self.hunt("h-items", "GET /items", candidate)
        self.assertEqual(code, 1)
        self.assertIn("must be propagation", out)
        self.assertIn("has no valid candidate; back to planned", out)
        self.assertEqual(self.unit("GET /items")["status"], "planned")
        self.assertEqual(self.read("candidates.json"), {})

    def test_a_unit_listed_twice_goes_back_to_planned(self):
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        reply = self.hunter_reply(cid, "h-items")
        reply["units"].append(dict(reply["units"][0], disposition="blocked", unresolved=["Gateway policy unknown."]))
        self.reply("h-items", reply)
        code, out, _ = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 1)
        self.assertIn("listed 2 times", out)
        self.assertEqual(self.unit("GET /items")["status"], "planned")

    def test_apply_before_extract_is_refused_and_keeps_the_reply_reachable(self):
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        code, _, err = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 2)
        self.assertIn("run extract", err)
        self.assertEqual(self.unit("GET /items")["status"], "in_progress")

    def test_paths_are_checked_never_rewritten(self):
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        reply = self.hunter_reply(cid, "h-items")
        reply["units"][0]["checks"][0]["reviewed_paths"] = [str(self.tmp / "target/src/api.js") + ":12-14"]
        self.reply("h-items", reply)
        code, out, _ = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 1)
        self.assertIn("is not repository-relative", out)
        self.assertEqual(self.unit("GET /items")["status"], "planned")

    def test_a_fingerprint_with_no_candidate_never_closes_a_unit(self):
        candidate = load_fixture("candidate-needs-validation.json")
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        reply = self.hunter_reply(cid, "h-items", candidate, candidate_fingerprints=["api/typo"])
        self.reply("h-items", reply)
        code, out, _ = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 1)
        self.assertIn("api/typo has no valid candidate", out)
        self.assertEqual(self.unit("GET /items")["status"], "planned")

    def test_a_unit_that_breaks_the_contract_goes_back_to_planned(self):
        code, out, _ = self.hunt("h-items", "GET /items", disposition="blocked")
        self.assertEqual(code, 1)
        self.assertIn("blocked unit needs unresolved facts", out)
        unit = self.unit("GET /items")
        self.assertEqual((unit["status"], unit["agent_id"], unit["local_checks"]), ("planned", None, []))

    def test_a_source_only_run_accepts_no_local_check(self):
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        reply = self.hunter_reply(cid, "h-items", disposition="covered")
        reply["units"][0]["checks"][0].update(method="local", artifact="agents/h-items/artifacts/out.txt")
        self.reply("h-items", reply)
        code, out, _ = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 1)
        self.assertIn("source-only run accepts no local check", out)

    def test_a_verifier_reply_with_a_field_outside_record_is_discarded_unrepaired(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        bad = {"decision": "needs_validation", "record": as_record(candidate), "fingerprint": candidate["fingerprint"]}
        (code, out, _), (applied, _, _) = self.verify("v-01", candidate["fingerprint"], bad)
        self.assertEqual((code, applied), (1, 1))
        self.assertIn("reply: unexpected field 'fingerprint'", out)
        self.assertFalse((self.run_dir / "agents/v-01/result.json").exists())
        self.assertTrue((self.run_dir / "agents/v-01/discarded-reply.txt").exists())
        self.assertIn("reply: unexpected field 'fingerprint'", self.call("v-01")["problems"])
        self.assertEqual(self.read("candidates.json")[candidate["fingerprint"]]["state"], "unvalidated")
        good = json.dumps({"decision": "needs_validation", "record": as_record(candidate)})
        (code, _, err), (applied, _, err2) = self.verify("v-02", candidate["fingerprint"], good)
        self.assertEqual((code, applied), (0, 0), err + err2)
        self.assertEqual([r["fingerprint"] for r in self.read("findings.json")], [candidate["fingerprint"]])
        unit = self.unit("GET /items")
        self.assertEqual([c["agent_id"] for c in unit["local_checks"]], ["h-items", "v-02"])
        self.assertEqual(unit["verifier_decisions"], {candidate["fingerprint"]: "needs_validation"})

    def test_a_reply_with_a_stray_brace_is_discarded(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        cli("prompt", "verifier", self.run_dir, "v-01", "--fingerprint", candidate["fingerprint"])
        code, out, _ = self.reply("v-01", json.dumps({"decision": "needs_validation", "record": as_record(candidate)}) + "}")
        self.assertEqual(code, 1)
        self.assertIn("not one JSON object", out)

    def test_a_fenced_reply_is_discarded_too(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        cli("prompt", "verifier", self.run_dir, "v-01", "--fingerprint", candidate["fingerprint"])
        fenced = "```json\n" + json.dumps({"decision": "needs_validation", "record": as_record(candidate)}) + "\n```"
        self.assertEqual(self.reply("v-01", fenced)[0], 1)
        code, _, err = self.reply("v-01", json.dumps({"decision": "needs_validation", "record": as_record(candidate)}))
        self.assertEqual(code, 2)
        self.assertIn("a re-run gets a fresh ID", err)

    def test_a_verifier_never_sees_another_verifiers_conclusion(self):
        first = load_fixture("candidate-needs-validation.json")
        second = dict(copy.deepcopy(first), fingerprint="api/items-list-without-owner-check")
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        reply = self.hunter_reply(cid, "h-items", first, candidate_fingerprints=[first["fingerprint"], second["fingerprint"]])
        reply["candidates"].append(second)
        self.reply("h-items", reply)
        self.assertEqual(cli("apply-hunter", self.run_dir, "h-items")[0], 0)
        self.verify("v-a", first["fingerprint"], {"decision": "needs_validation", "record": as_record(first)})
        self.assertIn("v-a", [k["agent_id"] for k in self.unit("GET /items")["local_checks"]])
        cli("prompt", "verifier", self.run_dir, "v-b", "--fingerprint", second["fingerprint"])
        text = (self.run_dir / "agents/v-b/prompt.md").read_text(encoding="utf-8")
        self.assertNotIn("Verifier decision", text)
        self.assertIn("No owner comparison before the response.", text)

    def test_a_new_fingerprint_from_a_verifier_waits_for_the_parent(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        other = dict(as_record(candidate), fingerprint="api/store-get-ignores-tenant")
        (code, _, _), (applied, out, _) = self.verify("v-01", candidate["fingerprint"], {"decision": "needs_validation", "record": other})
        self.assertEqual((code, applied), (0, 1))
        self.assertIn("--different-root-cause", out)
        self.assertEqual(self.call("v-01")["status"], "returned")
        code, out, err = cli("apply-verifier", self.run_dir, "v-01", "--different-root-cause")
        self.assertEqual(code, 0, err)
        candidates = self.read("candidates.json")
        self.assertEqual(candidates[candidate["fingerprint"]]["state"], "unvalidated")
        self.assertEqual(candidates["api/store-get-ignores-tenant"]["state"], "unvalidated")
        self.assertIn("api/store-get-ignores-tenant", self.unit("GET /items")["result_fingerprints"])

    def test_a_verifier_is_independent_and_every_id_is_fresh(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        code, _, err = cli("prompt", "verifier", self.run_dir, "h-items", "--fingerprint", candidate["fingerprint"])
        self.assertEqual(code, 2)
        self.assertIn("a verifier must be independent", err)
        code, _, err = cli("assign", self.run_dir, "h-items", self.unit("Search")["coverage_id"])
        self.assertEqual(code, 2)
        self.assertIn("fresh owner", err)

    def test_merge_puts_one_root_cause_under_one_fingerprint(self):
        first = load_fixture("candidate-needs-validation.json")
        second = dict(copy.deepcopy(first), fingerprint="search/items-read-without-owner-check")
        self.hunt("h-items", "GET /items", first)
        self.hunt("h-search", "Search", second)
        code, _, err = cli("merge", self.run_dir, first["fingerprint"], second["fingerprint"], "--reason", "Same store call.")
        self.assertEqual(code, 0, err)
        candidates = self.read("candidates.json")
        self.assertEqual(candidates[second["fingerprint"]]["state"], "merged")
        self.assertEqual([p["hunter"] for p in candidates[first["fingerprint"]]["proposals"]], ["h-items", "h-search"])
        search = self.unit("Search")
        self.assertEqual(search["result_fingerprints"], [first["fingerprint"]])
        self.assertEqual(search["duplicate_fingerprints"], [second["fingerprint"]])
        cli("prompt", "verifier", self.run_dir, "v-01", "--fingerprint", first["fingerprint"])
        self.assertIn("Proposal 2:", (self.run_dir / "agents/v-01/prompt.md").read_text(encoding="utf-8"))

    def test_the_quick_critic_defers_new_and_reopened_units(self):
        self.hunt("h-items", "GET /items")
        self.hunt("h-search", "Search")
        missing = {"surface": "POST /items", "boundary": "Item owner check", "subsystem": "API", "attack_class": "Access control",
                   "starting_paths": ["src/api.js"], "selected_companion_blocks": [], "excluded_blocks": [],
                   "reason": "No unit covers item writes."}
        elsewhere = dict(missing, surface="Admin export", starting_paths=["admin/export.js"], reason="Admin export is unmapped.")
        reply = {"missing_units": [missing, elsewhere], "reassign_ids": [self.unit("Search")["coverage_id"]],
                 "resolved_prior_leads": [], "stop": False}
        result = self.critic("c-final", reply, "--out-of-scope", "2=Not part of this run's scope.")
        self.assertEqual(len(result["accepted"]), 1)
        self.assertFalse(result["clean"])
        added = self.unit("POST /items")
        self.assertEqual(added["status"], "deferred")
        self.assertTrue(added["unresolved"][0].startswith("quick_profile_final_critic: "))
        self.assertEqual(self.unit("Admin export")["status"], "out_of_scope")
        reopened = self.unit("Search")
        self.assertEqual((reopened["status"], reopened["wave"], reopened["agent_id"]), ("deferred", 2, None))
        self.assertEqual(reopened["attempts"][0]["agent_id"], "h-search")
        self.assertEqual(reopened["attempts"][0]["status"], "covered")

    def test_a_critic_proposal_that_repeats_a_unit_is_skipped(self):
        self.hunt("h-items", "GET /items")
        self.hunt("h-search", "Search")
        repeat = {"surface": "Search", "boundary": "Query builder", "subsystem": runner.QUICK_SUBSYSTEM_LABEL,
                  "attack_class": "Injection", "starting_paths": ["src/search.js"], "selected_companion_blocks": [],
                  "excluded_blocks": [], "reason": "Search needs injection coverage."}
        result = self.critic("c-final", {"missing_units": [repeat], "reassign_ids": [], "resolved_prior_leads": [], "stop": False})
        self.assertEqual(result["already_in_ledger"], [self.unit("Search")["coverage_id"]])
        self.assertTrue(result["clean"])

    def test_a_quick_run_gets_one_critic_and_no_second_wave(self):
        self.hunt("h-items", "GET /items")
        self.critic("c-final")
        code, _, err = cli("prompt", "critic", self.run_dir, "c-again")
        self.assertEqual(code, 1)
        self.assertIn("exactly one hunter wave and one critic", err)
        code, _, err = cli("assign", self.run_dir, "h-late", self.unit("Search")["coverage_id"])
        self.assertEqual(code, 1)

    def test_the_quick_profile_has_no_separate_record_verifier(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        self.verify("v-01", candidate["fingerprint"], {"decision": "needs_validation", "record": as_record(candidate)})
        code, _, err = cli("prompt", "record-verifier", self.run_dir, "r-01", "--fingerprint", candidate["fingerprint"])
        self.assertEqual(code, 2)
        self.assertIn("merges Phase 5", err)
        self.assertIn("CHECKLIST-TEXT", (self.run_dir / "agents/v-01/prompt.md").read_text(encoding="utf-8"))

    def test_needs_validation_report_comes_from_the_records(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        self.critic("c-final")
        self.verify("v-01", candidate["fingerprint"], {"decision": "needs_validation", "record": as_record(candidate)})
        code, _, err = cli("needs-validation", self.run_dir)
        self.assertEqual(code, 0, err)
        text = (self.run_dir / "NEEDS-VALIDATION.md").read_text(encoding="utf-8")
        self.assertIn("## 1. Item lookup returns any item by id", text)
        self.assertIn("quick profile, source-only", text)
        self.assertIn("1. `entrypoint` `src/api.js:3` (getItem): Reads the id from the query string.", text)
        self.assertIn("- Whether the gateway in front of the service enforces item ownership is not in the repository.", text)
        self.assertIn("- Owner-observed: Ask the service owner", text)
        self.assertIn("- Item owner check (surface: GET /items)", text)
        self.assertNotIn("severity:", text.lower().replace("none has a severity", ""))

    def test_finish_names_open_work_and_records_an_incomplete_run(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        code, _, err = cli("finish", self.run_dir)
        self.assertEqual(code, 1)
        self.assertIn("unvalidated candidates", err)
        code, _, _ = cli("finish", self.run_dir, "--incomplete", "validation_budget_exhausted")
        self.assertEqual(code, 0)
        meta = self.read("run-metadata.json")
        self.assertEqual((meta["run_status"], meta["incomplete_reason"]), ("incomplete", "validation_budget_exhausted"))
        self.assertEqual(meta["unvalidated_candidates"], [candidate["fingerprint"]])
        self.assertEqual(meta["agents_spent"]["total"], 1)
        self.assertEqual(self.read("findings.json"), [])


class BudgetTest(RunCase):
    init_args = ("--budget", "8")

    def test_init_refuses_a_budget_that_cannot_fund_reconnaissance_and_reserves(self):
        code, _, err = cli("init", self.tmp / "run-2", "--target", self.tmp / "target", "--profile", "quick",
                           "--skill-dir", self.tmp / "skill", "--budget", "5")
        self.assertEqual(code, 1)
        self.assertIn("at least 6 agent calls", err)
        code, _, _ = cli("init", self.tmp / "run-2", "--target", self.tmp / "target", "--profile", "quick",
                         "--skill-dir", self.tmp / "skill", "--budget", "5", "--record-unfunded", "--without-validators")
        self.assertEqual(code, 0)
        code, out, _ = cli("finish", self.tmp / "run-2")
        self.assertEqual(code, 0)
        meta = json.loads((self.tmp / "run-2/run-metadata.json").read_text())
        self.assertEqual((meta["run_status"], meta["incomplete_reason"]),
                         ("incomplete", "budget_cannot_fund_reconnaissance_and_reserves"))

    def test_a_reserved_critic_is_never_refused(self):
        for agent in ("1a", "1b", "1c", "1d"):
            cli("prompt", "recon", self.run_dir, f"r-{agent}", "--agent", agent)
        first = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", first)
        self.hunt("h-search", "Search", dict(copy.deepcopy(first), fingerprint="search/other"))
        code, _, err = cli("prompt", "critic", self.run_dir, "c-final")
        self.assertEqual(code, 0, err)

    def test_hunters_stop_before_the_critic_and_validation_reserves(self):
        for agent in ("1a", "1b", "1c", "1d"):
            self.assertEqual(cli("prompt", "recon", self.run_dir, f"r-{agent}", "--agent", agent)[0], 0)
        self.assertEqual(json.loads(cli("budget", self.run_dir)[1])["hunters_affordable"], 2)
        self.assertIn("RECON-1a for " + str(self.tmp / "target"), (self.run_dir / "agents/r-1a/prompt.md").read_text())
        cli("assign", self.run_dir, "h-items", self.unit("GET /items")["coverage_id"])
        self.assertEqual(cli("prompt", "hunter", self.run_dir, "h-items")[0], 0)
        cli("assign", self.run_dir, "h-search", self.unit("Search")["coverage_id"])
        self.assertEqual(cli("prompt", "hunter", self.run_dir, "h-search")[0], 0)
        self.seed([dict(self.specs()[0], surface="PUT /items", refs={"surface": "src/api.js#putItem", "boundary": "src/api.js#ownerCheck"})])
        put = self.unit("PUT /items")["coverage_id"]
        code, _, err = cli("assign", self.run_dir, "h-put", put)
        self.assertEqual(code, 1)
        self.assertIn("budget 8: 6 spent", err)
        self.assertEqual(self.unit("PUT /items")["status"], "planned")
        approved = "The user approved one more hunter."
        self.assertEqual(cli("assign", self.run_dir, "h-put", put, "--over-budget", approved)[0], 0)
        code, _, _ = cli("prompt", "hunter", self.run_dir, "h-put", "--over-budget", approved)
        self.assertEqual(code, 0)
        self.assertEqual(self.call("h-put")["over_budget"], "The user approved one more hunter.")
        report = json.loads(cli("budget", self.run_dir)[1])
        self.assertEqual((report["spent"], report["remaining"], report["critics_owed"]), (7, 1, 1))


class StandardRunTest(RunCase):
    profile = "standard"

    def decided(self):
        candidate = load_fixture("candidate-needs-validation.json")
        self.hunt("h-items", "GET /items", candidate)
        self.hunt("h-search", "Search")
        self.assertTrue(self.critic("c-wave1")["clean"])
        self.critic("c-clean", final_clean=True)
        self.verify("v-01", candidate["fingerprint"], {"decision": "needs_validation", "record": as_record(candidate)})
        return candidate

    def record_verify(self, agent_id, fp, reply):
        code, _, err = cli("prompt", "record-verifier", self.run_dir, agent_id, "--fingerprint", fp)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.reply(agent_id, reply)[0], 0)
        code, out, err = cli("apply-record-verifier", self.run_dir, agent_id)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_a_standard_run_reserves_two_critics_per_wave(self):
        self.assertEqual(json.loads(cli("budget", self.run_dir)[1])["critics_owed"], 2)

    def test_a_material_replacement_waits_for_a_fresh_verifier(self):
        candidate = self.decided()
        fp = candidate["fingerprint"]
        self.assertEqual(self.read("candidates.json")[fp]["phase5"], "pending")
        stronger = load_fixture("record-confirmed.json")
        outcome = self.record_verify("r-01", fp, {"decision": "replace", "reason": "Reproduced locally.", "record": stronger})
        self.assertIn("material replacement", outcome["next"])
        self.assertEqual(self.read("findings.json")[0]["verdict"], "needs_validation")
        code, _, err = cli("prompt", "record-verifier", self.run_dir, "r-01b", "--fingerprint", fp)
        self.assertEqual(code, 0, err)
        self.assertIn("proposed replacement", (self.run_dir / "agents/r-01b/prompt.md").read_text(encoding="utf-8"))
        self.assertEqual(self.reply("r-01b", {"decision": "verified", "fingerprint": fp})[0], 0)
        self.assertEqual(cli("apply-record-verifier", self.run_dir, "r-01b")[0], 0)
        self.assertEqual(self.read("findings.json")[0]["verdict"], "confirmed")
        code, out, err = cli("finish", self.run_dir)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["run_status"], "complete")

    def test_a_replacement_naming_another_candidate_changes_nothing(self):
        candidate = self.decided()
        candidates = self.read("candidates.json")
        candidates["api/other"] = {"state": "confirmed", "verifiers": ["v-z"], "proposals": []}
        (self.run_dir / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
        cli("prompt", "record-verifier", self.run_dir, "r-01", "--fingerprint", candidate["fingerprint"])
        record = dict(as_record(candidate), fingerprint="api/other")
        self.assertEqual(self.reply("r-01", {"decision": "replace", "reason": "Other root cause.", "record": record})[0], 0)
        code, _, err = cli("apply-record-verifier", self.run_dir, "r-01")
        self.assertEqual(code, 2)
        self.assertIn("already a candidate", err)
        self.assertEqual(self.read("candidates.json")["api/other"]["verifiers"], ["v-z"])

    def test_a_wording_correction_applies_directly(self):
        candidate = self.decided()
        record = dict(as_record(candidate), description="The items handler returns any stored item for the id sent.")
        outcome = self.record_verify("r-01", candidate["fingerprint"], {"decision": "replace", "reason": "Wording.", "record": record})
        self.assertEqual(outcome["applied"], "non-material correction")
        self.assertEqual(self.read("candidates.json")[candidate["fingerprint"]]["phase5"], "verified")
        self.assertEqual(self.read("findings.json")[0]["description"], record["description"])


class CarriedRecordTest(RunCase):
    def setUp(self):
        prior = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(prior))
        (prior / "findings.json").write_text(json.dumps([load_fixture("record-confirmed.json")]), encoding="utf-8")
        (prior / "run-metadata.json").write_text(json.dumps({"profile": "quick"}), encoding="utf-8")
        self.prior = prior
        self.init_args = ("--prior", str(prior))
        super().setUp()

    def specs(self):
        units = super().specs()
        units[0]["prior_status"] = "prior_confirmed_same_source"
        return units

    def test_a_carried_confirmation_is_excluded_from_hunters_and_owned_by_its_verifier(self):
        fp = load_fixture("record-confirmed.json")["fingerprint"]
        carried = self.unit("GET /items")["coverage_id"]
        code, _, err = cli("carry", self.run_dir, "--prior-findings", self.prior / "findings.json", "--fingerprint", fp, "--unit", carried)
        self.assertEqual(code, 0, err)
        self.assertEqual(cli("assign", self.run_dir, "h-items", carried)[0], 2)
        cli("defer", self.run_dir, "--reason", "budget_cannot_reserve_critics_and_validation", "--all-planned")
        self.assertEqual(self.unit("GET /items")["status"], "planned")
        self.assertEqual(self.unit("Search")["status"], "deferred")
        self.seed([dict(super().specs()[1], surface="Search v2", refs={"surface": "src/search.js#search2", "boundary": "src/search.js#buildQuery"})])
        cli("assign", self.run_dir, "h-search", self.unit("Search v2")["coverage_id"])
        cli("prompt", "hunter", self.run_dir, "h-search")
        text = (self.run_dir / "agents/h-search/prompt.md").read_text(encoding="utf-8")
        self.assertIn(f"- {fp}: Item lookup returns another user's item: The handler calls the store", text)
        _, (applied, _, err) = self.verify("v-01", fp, {"decision": "confirmed", "record": load_fixture("record-confirmed.json")})
        self.assertEqual(applied, 0, err)
        unit = self.unit("GET /items")
        self.assertEqual((unit["status"], unit["agent_id"], unit["result_fingerprints"]), ("candidate", "v-01", [fp]))
        self.assertIn("Prior records with the same fingerprint\n\n```json", (self.run_dir / "agents/v-01/prompt.md").read_text())


class ValidatorTest(RunCase):
    validator_args = ()

    def setUp(self):
        if not runner.shutil.which("node"):
            self.skipTest("needs node")
        super_setup = super().setUp
        # Stand-in validators: the ledger one refuses any ledger holding the marker text.
        self.marker = "LEDGER-REJECT"
        original = write_skill

        def with_validators(folder):
            original(folder)
            (folder / "validate-coverage-ledger.cjs").write_text(
                "const t = require('fs').readFileSync(process.argv[2], 'utf8');\n"
                f"if (t.includes('{self.marker}')) {{ console.log('rejected marker'); process.exit(1); }}\n", encoding="utf-8")
            (folder / "validate-findings.cjs").write_text("process.exit(0);\n", encoding="utf-8")
        with mock.patch(__name__ + ".write_skill", with_validators):
            super_setup()

    def test_a_ledger_the_validator_refuses_sends_the_units_back_to_planned(self):
        cid = self.unit("GET /items")["coverage_id"]
        cli("assign", self.run_dir, "h-items", cid)
        cli("prompt", "hunter", self.run_dir, "h-items")
        reply = self.hunter_reply(cid, "h-items")
        reply["units"][0]["checks"][0]["result"] = f"{self.marker} in the result text."
        self.reply("h-items", reply)
        code, out, _ = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 1)
        self.assertIn("ledger validator refused", out)
        unit = self.unit("GET /items")
        self.assertEqual((unit["status"], unit["agent_id"]), ("planned", None))
        self.assertEqual(self.call("h-items")["status"], "applied")

    def test_a_run_with_validators_refuses_to_save_without_node(self):
        with mock.patch.object(runner.shutil, "which", return_value=None):
            code, _, err = cli("assign", self.run_dir, "h-items", self.unit("GET /items")["coverage_id"])
        self.assertEqual(code, 1)
        self.assertIn("cannot run: install node", err)
        self.assertEqual(self.unit("GET /items")["status"], "planned")


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd, "needs descriptor-relative no-follow opens")
class SandboxedRunTest(RunCase):
    init_args = ("--sandbox", "a fixture sandbox with no network")

    def setUp(self):
        patcher = mock.patch.object(runner.sys, "platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        cli("assign", self.run_dir, "h-items", self.unit("GET /items")["coverage_id"])
        code, _, err = cli("prompt", "hunter", self.run_dir, "h-items", "--allow-artifact", "out/result.txt=64",
                           "--allow-artifact", "big.txt=4", "--allow-artifact", "link.txt=64", "--allow-artifact", "hard.txt=64")
        self.assertEqual(code, 0, err)
        self.scratch = self.run_dir / "agents/h-items/scratch"

    def test_macos_runs_are_source_only(self):
        with mock.patch.object(runner.sys, "platform", "darwin"):
            code, _, err = cli("init", self.tmp / "run-mac", "--target", self.tmp / "target", "--skill-dir", self.tmp / "skill",
                               "--sandbox", "anything")
        self.assertEqual(code, 2)
        self.assertIn("source-only", err)

    def test_a_promoted_file_backs_a_local_check(self):
        text = (self.run_dir / "agents/h-items/prompt.md").read_text(encoding="utf-8")
        self.assertIn("- out/result.txt: at most 64 bytes", text)
        self.assertNotIn("using source review only", text)
        (self.scratch / "out").mkdir()
        (self.scratch / "out/result.txt").write_text("item 2 returned to user 1\n", encoding="utf-8")
        code, out, err = cli("promote", self.run_dir, "h-items", "out/result.txt")
        self.assertEqual(code, 0, err)
        artifact = json.loads(out)["artifact"]
        self.assertEqual((self.run_dir / artifact).read_text(encoding="utf-8"), "item 2 returned to user 1\n")
        reply = self.hunter_reply(self.unit("GET /items")["coverage_id"], "h-items", disposition="covered")
        reply["units"][0]["checks"][0].update(method="local", artifact=artifact)
        self.reply("h-items", reply)
        code, out, _ = cli("apply-hunter", self.run_dir, "h-items")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.unit("GET /items")["local_checks"][0]["artifact"], artifact)

    def test_promotion_refuses_links_oversize_files_and_unlisted_paths(self):
        (self.scratch / "big.txt").write_text("too long", encoding="utf-8")
        (self.scratch / "real.txt").write_text("x", encoding="utf-8")
        os.symlink(self.scratch / "real.txt", self.scratch / "link.txt")
        os.link(self.scratch / "real.txt", self.scratch / "hard.txt")
        for path, expected in (("big.txt", "over its limit"), ("link.txt", "failed"), ("hard.txt", "single-link"),
                               ("real.txt", "not on")):
            code, _, err = cli("promote", self.run_dir, "h-items", path)
            self.assertEqual(code, 2, path)
            self.assertIn(expected, err, path)
        self.assertEqual(list((self.run_dir / "agents/h-items/artifacts").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
