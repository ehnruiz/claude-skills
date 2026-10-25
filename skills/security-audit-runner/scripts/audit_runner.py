#!/usr/bin/env python3
"""Parent-side runner for Cloudflare's security-audit skill in full audit mode.

Usage: audit_runner.py COMMAND [RUN_DIR] [options]. `audit_runner.py COMMAND -h` lists a command's options.

The audit method is Cloudflare's (https://github.com/cloudflare/security-audit-skill). This script never carries
its text: it reads the installed skill at run time, copies its blocks into each agent's prompt file verbatim, checks
each agent's reply against the contract that prompt gave it, and keeps the parent-owned run files
(coverage-ledger.json, candidates.json, findings.json, agent-calls.json, NEEDS-VALIDATION.md).

Exit codes: 0 done, 1 refused (a reply was discarded, the budget or a validator said no), 2 usage or input error.
"""

import argparse
import json
import math
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
import urllib.parse

TESTED_COMMIT = "c1c8a8c"
DEFAULT_SKILL_DIR = "~/.claude/skills/security-audit"
QUICK_SUBSYSTEM = "profile/quick/all-in-scope-subsystems"
QUICK_SUBSYSTEM_LABEL = "All in-scope subsystems (quick profile)"
PROFILES = ("quick", "standard", "deep")
ROLES = ("recon", "hunter", "critic", "verifier", "record-verifier")
MODELS = {"recon": "sonnet", "hunter": "sonnet", "critic": "opus", "verifier": "opus", "record-verifier": "opus"}
RECON_AGENTS = ("1a", "1b", "1c", "1d")
AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEVICE_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
FINGERPRINT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
BLOCK_FILE = re.compile(r"^[A-Z0-9][A-Z0-9-]*\.md$")
WINDOWS_RESERVED = re.compile(r"^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)
COMPANION_SECTIONS = ("Core discipline", "Universal moves", "Validation rules")
VERDICTS = ("confirmed", "needs_validation", "rejected")
VERDICT_RANK = {"rejected": 0, "needs_validation": 1, "confirmed": 2}
SEVERITY_RANK = {"informational": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
CLOSED = ("blocked", "covered", "candidate")
NO_OWNER = ("planned", "not_applicable", "out_of_scope", "deferred")
VR = "VALIDATION-AND-REPORTING.md"
_MISSING = object()


class RunnerError(Exception):
    """Bad input, reported on stderr with exit code 2."""

    code = 2


class Refused(RunnerError):
    """The budget, a validator or an unfinished run said no: exit code 1."""

    code = 1


def note(message):
    print(f"note: {message}", file=sys.stderr)


# ---------------------------------------------------------------- text rules shared with Cloudflare's validators


def _ignorable(ch):
    """Control, format, line or paragraph separator, or default-ignorable code point."""
    cp = ord(ch)
    return (unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") or cp == 0x034F or 0x115F <= cp <= 0x1160
            or 0x17B4 <= cp <= 0x17B5 or 0x180B <= cp <= 0x180F or cp == 0x3164 or 0xFE00 <= cp <= 0xFE0F
            or cp == 0xFFA0 or 0xFFF0 <= cp <= 0xFFF8 or 0x1BCA0 <= cp <= 0x1BCA3 or 0x1D173 <= cp <= 0x1D17A
            or 0xE0000 <= cp <= 0xE0FFF)


def visible(text):
    return isinstance(text, str) and any(not ch.isspace() and not _ignorable(ch) for ch in text)


def safe_path(value):
    """The findings validator's rule for a repository-relative source path."""
    if not isinstance(value, str) or not value or value.strip() != value or "\\" in value or ":" in value:
        return False
    if value.startswith(("/", "~")) or any(_ignorable(ch) for ch in value):
        return False
    return all(seg not in ("", ".", "..") and not seg.endswith((" ", ".")) and not WINDOWS_RESERVED.match(seg)
               for seg in value.split("/"))


def ledger_text(value, limit=4096):
    """The ledger validator's rule for text: trimmed, visible, at most `limit` UTF-16 code units."""
    return (isinstance(value, str) and value.strip() == value and visible(value)
            and len(value.encode("utf-16-le")) // 2 <= limit)


def safe_agent_id(value):
    return isinstance(value, str) and bool(AGENT_ID.match(value)) and value not in DEVICE_NAMES


def encode_ref(ref):
    """RFC 3986 encoding of one canonical reference, as RECONNAISSANCE.md derives coverage_id."""
    if (not isinstance(ref, str) or ref != ref.strip() or ref != unicodedata.normalize("NFC", ref)
            or not visible(ref) or any(_ignorable(ch) for ch in ref)):
        raise RunnerError(f"canonical reference {ref!r} must be NFC, trimmed, visible and free of control characters")
    return urllib.parse.quote(ref, safe="-._~")


def coverage_id(refs):
    keys = ["surface", "boundary", "subsystem", "attack_class"] + (["lifecycle"] if "lifecycle" in refs else [])
    return "::".join(encode_ref(refs[k]) for k in keys)


# ---------------------------------------------------------------- JSON files


def read_json(path, default=_MISSING):
    path = pathlib.Path(path)
    if not path.exists():
        if default is not _MISSING:
            return default
        raise RunnerError(f"{path} does not exist")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RunnerError(f"{path} is not valid JSON: {error}")


def write_json(path, data):
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the installed skill


class Skill:
    """Read access to the installed security-audit skill. Every block comes out verbatim."""

    def __init__(self, directory):
        self.dir = pathlib.Path(directory).expanduser().resolve()
        head = self.dir / "SKILL.md"
        if not head.is_file() or not re.search(r"^name:\s*security-audit\s*$", head.read_text(encoding="utf-8"), re.M):
            raise RunnerError(f"no security-audit skill at {self.dir}; install it or pass --skill-dir")
        self._cache = {}

    def text(self, name):
        if name != "report-schema.json" and not BLOCK_FILE.match(name):
            raise RunnerError(f"{name!r} is not a file name of the security-audit skill")
        if name not in self._cache:
            path = self.dir / name
            if not path.is_file():
                raise RunnerError(f"{path} does not exist")
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]

    def _find(self, name, anchor, start=0):
        index = self.text(name).find(anchor, start)
        if index < 0:
            raise RunnerError(f"{name} has no {anchor!r}; this runner was tested with security-audit at "
                              f"{TESTED_COMMIT}, so check which version is installed")
        return index

    def fenced_after(self, name, anchor):
        """The first fenced block after the first line containing `anchor`."""
        match = re.compile(r"```[a-z]*\n(.*?)\n```", re.S).search(self.text(name), self._find(name, anchor))
        if not match:
            raise RunnerError(f"{name} has no fenced block after {anchor!r}")
        return match.group(1)

    def between(self, name, start, end):
        i = self._find(name, start)
        return self.text(name)[i:self._find(name, end, i)].rstrip()

    def section(self, name, title):
        """A `## title` section, up to the next `## ` heading."""
        match = re.search(r"^## " + re.escape(title) + r".*?(?=^## |\Z)", self.text(name), re.S | re.M)
        if not match:
            raise RunnerError(f"{name} has no section {title!r}")
        return match.group(0).rstrip()

    def class_block(self, name, title):
        """A class paragraph: its bold name alone on a line (optionally with a subagent_type note), up to the next one."""
        line = r"\*\*{}\*\*(?: \(subagent_type[^\n]*\))?[ \t]*$"
        match = re.search(r"^" + line.format(re.escape(title)) + r".*?(?=^" + line.format(r"[^*\n]+") + r"|^#+ |\Z)",
                          self.text(name), re.S | re.M)
        if not match:
            raise RunnerError(f"{name} has no attack class {title!r}")
        return match.group(0).rstrip()

    def block(self, ref):
        """Resolve FILE.md#Name to its verbatim text."""
        file, sep, title = ref.partition("#") if isinstance(ref, str) else ("", "", "")
        if not sep or not title or not BLOCK_FILE.match(file):
            raise RunnerError(f"{ref!r} is not a block reference of the form FILE.md#Name")
        return self.section(file, title) if title in COMPANION_SECTIONS else self.class_block(file, title)

    def branches(self):
        """report-schema.json's verdict branches, by verdict."""
        schema = json.loads(self.text("report-schema.json"))
        return {b["properties"]["verdict"]["const"]: b for b in schema["items"]["oneOf"]}

    def branch_text(self, *verdicts):
        branches = self.branches()
        return "\n\n".join(json.dumps(branches[v], indent=2, ensure_ascii=False) for v in verdicts)

    def commit(self):
        result = subprocess.run(["git", "-C", str(self.dir), "rev-parse", "HEAD"], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None


def run_validator(skill, script, path):
    """Run one of the skill's Node validators: (exit code, output), or None when node or the script is missing."""
    js, node = skill.dir / script, shutil.which("node")
    if not node or not js.is_file():
        return None
    result = subprocess.run([node, str(js), str(path)], capture_output=True, text=True, timeout=300)
    return result.returncode, (result.stdout + result.stderr).strip()


# ---------------------------------------------------------------- record checks (a subset of validate-findings.cjs)

TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def schema_errors(value, schema, where="record"):
    """Check `value` against one schema node, using only the keywords report-schema.json uses."""
    errors = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    kind = schema.get("type")
    if kind and not TYPES[kind](value):
        return errors + [f"{where}: expected {kind}"]
    if isinstance(value, dict):
        errors += [f"{where}: missing field {k!r}" for k in schema.get("required", []) if k not in value]
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                errors += schema_errors(item, properties[key], f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unexpected field {key!r}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            errors += [f"{where}[{i}]: duplicate item" for i, s in enumerate(seen) if s in seen[:i]]
        if "items" in schema:
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], f"{where}[{i}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where}: needs at least {schema['minLength']} character(s)")
        if schema.get("visibleContent") and not visible(value):
            errors.append(f"{where}: must contain a visible character")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: must match {schema['pattern']}")
    if TYPES["number"](value) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{where}: must be at least {schema['minimum']}")
    return errors


def trace_errors(trace):
    if not isinstance(trace, list) or not trace or not all(isinstance(e, dict) for e in trace):
        return []
    kinds = [e.get("kind") for e in trace]
    if len(kinds) == 1:
        return [] if kinds[0] in ("entrypoint", "sink") else ["record.trace[0].kind: a one-entry trace is entrypoint or sink"]
    errors = [] if kinds[0] == "entrypoint" else [f"record.trace[0].kind: must be entrypoint, got {kinds[0]!r}"]
    if kinds[-1] != "sink":
        errors.append(f"record.trace[{len(kinds) - 1}].kind: must be sink, got {kinds[-1]!r}")
    errors += [f"record.trace[{i}].kind: must be propagation, got {k!r}"
               for i, k in enumerate(kinds[1:-1], 1) if k != "propagation"]
    return errors


def record_errors(record, branches):
    """Schema-branch and semantic errors for one findings.json record; empty when the record is acceptable."""
    if not isinstance(record, dict):
        return ["record: expected an object"]
    verdict = record.get("verdict")
    if verdict not in branches:
        return [f"record.verdict: {verdict!r} is not one of {sorted(branches)}"]
    errors = schema_errors(record, branches[verdict])
    for field in ("trace", "evidence"):
        for i, entry in enumerate(record.get(field) if isinstance(record.get(field), list) else []):
            if isinstance(entry, dict) and "file" in entry and not safe_path(entry["file"]):
                errors.append(f"record.{field}[{i}].file: must be a safe repository-relative path")
    remediation = record.get("remediation")
    changes = remediation.get("code_changes") if isinstance(remediation, dict) else None
    for i, change in enumerate(changes if isinstance(changes, list) else []):
        if isinstance(change, dict) and "file_name" in change and not safe_path(change["file_name"]):
            errors.append(f"record.remediation.code_changes[{i}].file_name: must be a safe repository-relative path")
    errors += trace_errors(record.get("trace"))
    severity = record.get("severity")
    if verdict == "confirmed" and isinstance(severity, dict) and isinstance(severity.get("impact"), dict):
        overall, impact = severity.get("overall_severity"), severity["impact"].get("score")
        if overall in SEVERITY_RANK and impact in SEVERITY_RANK and SEVERITY_RANK[overall] > SEVERITY_RANK[impact]:
            errors.append(f"record.severity.overall_severity: {overall} exceeds the demonstrated impact {impact}")
    plan = record.get("validation_plan")
    if verdict == "needs_validation" and not (isinstance(plan, dict) and (visible(plan.get("local")) or visible(plan.get("deployment")))):
        errors.append("record.validation_plan: needs a visible local or deployment plan")
    return errors


def draft_errors(candidate, branches):
    """Errors in a hunter candidate, read as the record it proposes (proposed_verdict in place of verdict)."""
    if not isinstance(candidate, dict):
        return ["candidate: expected an object"]
    draft = {k: v for k, v in candidate.items() if k != "proposed_verdict"}
    if "verdict" in candidate:
        return ["candidate: uses verdict; a hunter proposes with proposed_verdict"]
    draft["verdict"] = candidate.get("proposed_verdict")
    return record_errors(draft, branches)


def shape_errors(obj, fields, where):
    if not isinstance(obj, dict):
        return [f"{where}: expected a JSON object"]
    errors = [f"{where}: missing field {k!r}" for k in fields if k not in obj]
    errors += [f"{where}: unexpected field {k!r}" for k in obj if k not in fields]
    return errors + [f"{where}.{k}: expected {t.__name__}" for k, t in fields.items() if k in obj and not isinstance(obj[k], t)]


def strings_errors(values, where):
    return [f"{where}[{i}]: expected a string" for i, v in enumerate(values) if not isinstance(v, str)]


# ---------------------------------------------------------------- the run directory


class Run:
    def __init__(self, root):
        self.root = pathlib.Path(root).expanduser().resolve()
        self.meta = read_json(self.root / "run-metadata.json")
        self.skill = Skill(self.meta["skill"]["dir"])
        self._branches = None

    @property
    def profile(self):
        return self.meta["profile"]

    @property
    def target(self):
        return self.meta["target"]

    @property
    def sandbox(self):
        return self.meta.get("execution") == "sandboxed-local"

    def file(self, name):
        return self.root / name

    def agent_dir(self, agent_id):
        return self.root / "agents" / agent_id

    def branches(self):
        if self._branches is None:
            self._branches = self.skill.branches()
        return self._branches

    def ledger(self):
        return read_json(self.file("coverage-ledger.json"), [])

    def candidates(self):
        return read_json(self.file("candidates.json"), {})

    def calls(self):
        return read_json(self.file("agent-calls.json"), {"calls": [], "validation_reserve": None})

    def call(self, calls, agent_id, role=None):
        found = next((c for c in calls["calls"] if c["agent_id"] == agent_id), None)
        if found is None:
            raise RunnerError(f"no agent {agent_id} in agent-calls.json; build its prompt first")
        if role and found["role"] != role:
            raise RunnerError(f"{agent_id} is a {found['role']}, not a {role}")
        return found

    def architecture(self):
        path = self.file("architecture.md")
        if not path.is_file():
            raise RunnerError(f"{path} does not exist; write it from the reconnaissance results first")
        return path.read_text(encoding="utf-8").strip()

    def findings_from(self, candidates):
        records = [c["final_record"] for c in candidates.values()
                   if c.get("state") in VERDICTS and c.get("final_record") and not c.get("removed")]
        return sorted(records, key=lambda r: r["fingerprint"])

    def save(self, ledger=None, candidates=None, calls=None, findings=False, extra=()):
        """Validate the ledger and findings with the skill's validators before writing anything."""
        staged = []
        if ledger is not None:
            ids = [u["coverage_id"] for u in ledger]
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            if duplicates:
                raise RunnerError(f"duplicate coverage_id {duplicates[0]}; fix the canonical refs, never merge")
            ledger.sort(key=lambda u: u["coverage_id"])
            staged.append(("coverage-ledger.json", ledger, "validate-coverage-ledger.cjs"))
        if findings:
            staged.append(("findings.json", self.findings_from(candidates), "validate-findings.cjs"))
        for name, data, validator in staged:
            check = self.file(name.replace(".json", ".check.json"))
            write_json(check, data)
            result = self.validator(validator, check)
            if result is not None and result[0] != 0:
                for other, _, _ in staged:
                    self.file(other.replace(".json", ".check.json")).unlink(missing_ok=True)
                raise Refused(f"{name} failed {validator}, so nothing was saved:\n{result[1]}")
        for name, _, _ in staged:
            os.replace(self.file(name.replace(".json", ".check.json")), self.file(name))
        if candidates is not None:
            write_json(self.file("candidates.json"), candidates)
        if calls is not None:
            write_json(self.file("agent-calls.json"), calls)
        for name, data in extra:
            write_json(self.file(name), data)

    def validator(self, script, path):
        """Run one of the skill's validators. A run started with --without-validators skips them; any other run refuses."""
        result = run_validator(self.skill, script, path)
        if result is None:
            if self.meta.get("validators") != "skipped":
                raise Refused(f"{script} cannot run: install node, or check that the skill has its validators")
            note(f"{script} skipped: the run was started with --without-validators")
        return result

    def save_meta(self):
        write_json(self.file("run-metadata.json"), self.meta)


def owners_in(ledger):
    owners = set()
    for unit in ledger:
        owners.add(unit.get("agent_id"))
        owners.update(a.get("agent_id") for a in unit.get("attempts", []))
        for check in unit.get("local_checks", []):
            owners.add(check.get("agent_id"))
    owners.discard(None)
    return owners


def fresh_id(run, agent_id, calls, ledger, candidates):
    if not safe_agent_id(agent_id):
        raise RunnerError(f"{agent_id!r} is not a canonical agent ID: lowercase, ^[a-z0-9][a-z0-9_-]{{0,63}}$, not a device name")
    used = {c["agent_id"] for c in calls["calls"]} | owners_in(ledger)
    for c in candidates.values():
        used.update(c.get("verifiers", []) + c.get("record_verifiers", []))
    if agent_id in used:
        raise RunnerError(f"agent ID {agent_id} was already used in this run; every assignment gets a fresh owner")


def companion_files(units):
    return sorted({ref.split("#", 1)[0] for u in units for ref in u.get("selected_companion_blocks", [])})


def linked_units(candidate):
    """Units a candidate's proposals close, plus units that point at it without proposing it."""
    return sorted({cid for p in candidate.get("proposals", []) for cid in p.get("units", [])} | set(candidate.get("linked_units", [])))


def resolve_fp(candidates, fp):
    """Follow merges to the live candidate entry for `fp`."""
    seen = set()
    while fp in candidates and candidates[fp].get("state") == "merged" and fp not in seen:
        seen.add(fp)
        fp = candidates[fp]["merged_into"]
    return fp


# ---------------------------------------------------------------- budget


def hunting_wave(calls):
    """The wave a new hunter joins: the latest one, or the next once a critic has reviewed the latest."""
    latest = max([c["wave"] for c in calls["calls"] if c["role"] == "hunter"] or [0])
    if latest == 0:
        return 1
    reviewed = any(c["role"] == "critic" and c.get("wave") == latest for c in calls["calls"])
    return latest + 1 if reviewed else latest


def critics_owed(run, calls, wave=None):
    hunters = [c for c in calls["calls"] if c["role"] == "hunter"]
    critics = [c for c in calls["calls"] if c["role"] == "critic"]
    if run.profile == "quick":
        return 0 if critics else 1
    current = max([c.get("wave", 1) for c in hunters] + [wave or 1])
    post = any(c.get("wave") == current and not c.get("final_clean") for c in critics)
    final = any(c.get("wave") == current and c.get("final_clean") for c in critics)
    return (0 if post else 1) + (0 if final else 1)


def verifier_need(run, candidates):
    per = 1 if run.profile == "quick" else 2
    need = 0
    for c in candidates.values():
        if c.get("state") == "unvalidated":
            need += per
        elif run.profile != "quick" and c.get("state") in ("confirmed", "needs_validation") and c.get("phase5") != "verified":
            need += 1
    return need


def default_reserve(budget, spent, owed_critics):
    return max(1, math.ceil(0.3 * (budget - spent - owed_critics)))


def budget_gap(run, calls, candidates, role, wave=None):
    """Why one more `role` call does not fit the budget with its reserves, or None when it fits."""
    if run.profile == "quick" and any(c["role"] == "critic" for c in calls["calls"]) and role in ("hunter", "critic"):
        return f"a quick run has exactly one hunter wave and one critic, and its critic has run; no {role} follows it"
    budget = run.meta.get("budget")
    if budget is None:
        return None
    spent = len(calls["calls"])
    owed_critics = critics_owed(run, calls, wave)
    if role == "recon":
        owed = owed_critics + 1
    elif role == "hunter":
        reserve = calls.get("validation_reserve")
        reserve = default_reserve(budget, spent, owed_critics) if reserve is None else reserve
        owed = owed_critics + max(reserve, verifier_need(run, candidates))
    elif role == "critic":
        owed = max(0, owed_critics - 1)
    else:
        owed = 0
    if spent + 1 + owed <= budget:
        return None
    return (f"budget {budget}: {spent} spent, this {role} would leave {budget - spent - 1} for {owed} reserved "
            "(critics and validation)")


def budget_report(run, calls, candidates):
    budget = run.meta.get("budget")
    spent = len(calls["calls"])
    by_role = {r: sum(c["role"] == r for c in calls["calls"]) for r in ROLES}
    owed = critics_owed(run, calls, hunting_wave(calls))
    report = {"budget": budget, "spent": spent, "by_role": by_role, "critics_owed": owed,
              "verifiers_needed_now": verifier_need(run, candidates), "validation_reserve": calls.get("validation_reserve")}
    if budget is not None:
        reserve = calls.get("validation_reserve")
        reserve = default_reserve(budget, spent, owed) if reserve is None else reserve
        report["remaining"] = budget - spent
        report["hunters_affordable"] = max(0, budget - spent - owed - max(reserve, report["verifiers_needed_now"]))
    if run.profile == "quick" and by_role["critic"]:
        report["hunters_affordable"] = 0
    return report


def register(run, calls, candidates, agent_id, role, over_budget=None, **fields):
    """Count one agent call against the budget, keeping the critic and validation reserves intact."""
    existing = next((c for c in calls["calls"] if c["agent_id"] == agent_id), None)
    if existing:
        if existing["role"] != role or existing["status"] != "built":
            raise RunnerError(f"{agent_id} is already a {existing['role']} call with status {existing['status']}; use a fresh ID")
        existing.update(fields)
        return existing
    entry = dict(agent_id=agent_id, role=role, model=MODELS[role], status="built", **fields)
    gap = budget_gap(run, calls, candidates, role, fields.get("wave"))
    if gap and gap.startswith("a quick run"):
        raise Refused(gap)
    if gap:
        if not over_budget:
            raise Refused(gap + ". Ask the user, or pass --over-budget REASON once they agree.")
        entry["over_budget"] = over_budget
        note(gap + f"; recorded as over budget: {over_budget}")
    budget = run.meta.get("budget")
    if role == "hunter" and budget is not None and calls.get("validation_reserve") is None:
        calls["validation_reserve"] = default_reserve(budget, len(calls["calls"]), critics_owed(run, calls, fields.get("wave")))
        note(f"validation reserve set to {calls['validation_reserve']} agent calls (30% of the balance after critics)")
    calls["calls"].append(entry)
    return entry


# ---------------------------------------------------------------- init, seed, carry, assign, defer


def default_run_dir(repo):
    base = pathlib.Path("~/security-audit-skill").expanduser() / repo.split("/")[-1]
    n = 1
    while (base / f"run-{n}").exists():
        n += 1
    return base / f"run-{n}"


def git(target, *args):
    result = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def repo_name(target):
    remote = git(target, "remote", "get-url", "origin")
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote or "")
    return match.group(1) if match else target.name


def cmd_init(args):
    target = pathlib.Path(args.target).expanduser().resolve()
    if not target.is_dir():
        raise RunnerError(f"{target} is not a directory")
    skill = Skill(args.skill_dir)
    repo = args.repo or repo_name(target)
    root = pathlib.Path(args.run).expanduser().resolve() if args.run else default_run_dir(repo)
    if root.exists() and any(root.iterdir()):
        raise RunnerError(f"{root} is not empty; every run gets a new output directory")
    if root == target or target in root.parents:
        ignored = subprocess.run(["git", "-C", str(target), "check-ignore", "-q", str(root)]).returncode == 0
        if not (args.inside_target and ignored):
            raise RunnerError(f"{root} is inside the target; use an outside path, or pass --inside-target for a directory "
                              "the user chose and version control ignores")
    if args.sandbox and sys.platform == "darwin":
        raise RunnerError("macOS cannot enforce the skill's sandbox controls, so a run here is source-only; drop --sandbox")
    minimum = 4 + (1 if args.profile == "quick" else 2) + 1
    if args.budget is not None and args.budget < minimum and not args.record_unfunded:
        raise Refused(f"a {args.profile} run needs at least {minimum} agent calls (4 reconnaissance, the critics, one "
                          "verifier); ask the user for a larger budget, a narrower scope or another profile")
    commit = git(target, "rev-parse", "HEAD")
    status = git(target, "status", "--porcelain")
    priors = []
    for prior in args.prior or []:
        prior = pathlib.Path(prior).expanduser().resolve()
        meta = read_json(prior / "run-metadata.json", {})
        priors.append({"path": str(prior), "source_ref": meta.get("source_ref"), "profile": meta.get("profile"),
                       "scope_paths": meta.get("scope_paths"), "ledger": (prior / "coverage-ledger.json").is_file(),
                       "findings": (prior / "findings.json").is_file()})
    meta = {
        "run_id": f"{repo.split('/')[-1]}-{root.name}",
        "repo": repo,
        "target": str(target),
        "source_ref": {"commit": commit, "dirty": bool(status) if status is not None else None},
        "profile": args.profile,
        "scope_paths": args.scope or [],
        "budget": args.budget,
        "execution_policy": "sandboxed-source-and-local-only",
        "execution": "sandboxed-local" if args.sandbox else "source-only",
        "sandbox": args.sandbox,
        "skill": {"repo": "cloudflare/security-audit-skill", "commit": skill.commit(), "dir": str(skill.dir)},
        "selected_companions": [],
        "prior_runs": priors,
        "shared_file_owner": "parent",
        "run_status": "in_progress",
    }
    if args.without_validators:
        meta["validators"] = "skipped"
    if args.budget is not None and args.budget < minimum:
        meta.update(run_status="incomplete", incomplete_reason="budget_cannot_fund_reconnaissance_and_reserves")
    (root / "agents").mkdir(parents=True, exist_ok=True)
    write_json(root / "run-metadata.json", meta)
    write_json(root / "agent-calls.json", {"calls": [], "validation_reserve": None})
    print(json.dumps({"run": str(root), "profile": args.profile, "execution": meta["execution"],
                      "run_status": meta["run_status"], "skill_commit": meta["skill"]["commit"]}, indent=1))


def companion_refs(file, classes):
    return [f"{file}#Core discipline"] + [f"{file}#{c}" for c in classes] + [f"{file}#Universal moves", f"{file}#Validation rules"]


def build_unit(run, spec, index):
    """One ledger unit from a seed spec entry; see SKILL.md for the spec fields."""
    where = f"spec[{index}]"
    if not isinstance(spec, dict):
        raise RunnerError(f"{where}: expected an object")
    for key in ("surface", "boundary", "attack_class", "refs"):
        if key not in spec:
            raise RunnerError(f"{where}: missing {key!r}")
    refs = dict(spec["refs"])
    quick = run.profile == "quick"
    if quick:
        if refs.setdefault("subsystem", QUICK_SUBSYSTEM) != QUICK_SUBSYSTEM:
            raise RunnerError(f"{where}: a quick run uses the subsystem ref {QUICK_SUBSYSTEM}")
    companions = list(spec.get("companion_blocks", []))
    for file, classes in sorted(spec.get("companions", {}).items()):
        companions += [r for r in companion_refs(file, classes) if r not in companions]
    ordinary = spec.get("ordinary_block")
    refs.setdefault("attack_class", ordinary or next((r for r in companions if r.split("#", 1)[1] not in COMPANION_SECTIONS), None))
    missing = [k for k in ("surface", "boundary", "subsystem", "attack_class") if not refs.get(k)]
    if missing:
        raise RunnerError(f"{where}: refs needs {missing}")
    excluded = list(spec.get("excluded_blocks", []))
    for ref in ([ordinary] if ordinary else []) + companions + [e.get("block") for e in excluded]:
        run.skill.block(ref)
    status = spec.get("status", "planned")
    if status not in NO_OWNER:
        raise RunnerError(f"{where}: a seeded unit is planned, not_applicable, out_of_scope or deferred, not {status!r}")
    reason = spec.get("reason")
    if (status == "planned") == bool(reason):
        raise RunnerError(f"{where}: a {status} unit {'takes no' if status == 'planned' else 'needs a'} reason")
    paths = [p for p in spec.get("starting_paths", [])]
    bad = [p for p in paths if not safe_path(p)]
    if bad:
        raise RunnerError(f"{where}: starting paths must be repository-relative: {bad}")
    unit = {
        "coverage_id": coverage_id(refs),
        "canonical_refs": refs,
        "surface": spec["surface"],
        "boundary": spec["boundary"],
        "subsystem": spec.get("subsystem", QUICK_SUBSYSTEM_LABEL if quick else None),
        "attack_class": spec["attack_class"],
        "starting_paths": paths,
        "ordinary_attack_class_block": ordinary,
        "selected_companion_blocks": companions,
        "excluded_blocks": excluded,
        "prior_status": spec.get("prior_status", "new" if any(p.get("ledger") for p in run.meta.get("prior_runs", [])) else "none"),
        "attempts": [],
        "wave": 1,
        "status": status,
        "agent_id": None,
        "reviewed_paths": [],
        "local_checks": [],
        "result_fingerprints": [],
        "unresolved": [reason] if reason else [],
    }
    if unit["subsystem"] is None:
        raise RunnerError(f"{where}: missing 'subsystem' label")
    if "lifecycle" in refs:
        unit["lifecycle"] = spec.get("lifecycle") or refs["lifecycle"]
    for key in ("priority", "rationale"):
        if key in spec:
            unit["assignment_" + key] = spec[key]
    return unit


SEMANTIC = ("canonical_refs", "surface", "boundary", "subsystem", "attack_class", "ordinary_attack_class_block",
            "selected_companion_blocks", "excluded_blocks")


def label_key(unit):
    return (unit["surface"], unit["boundary"], unit["subsystem"], unit["attack_class"], unit.get("lifecycle"))


def add_units(ledger, units, skip_repeats=False):
    """Append units. A repeat (same ID or same labels) fails, or is skipped with `skip_repeats`; a canonical ID
    collision (same ID, different labels) always fails."""
    by_id = {u["coverage_id"]: u for u in ledger}
    by_label = {label_key(u): u for u in ledger}
    added, repeats = [], []
    for unit in units:
        same = by_id.get(unit["coverage_id"])
        if same is not None and label_key(same) != label_key(unit):
            raise RunnerError(f"canonical ID collision on {unit['coverage_id']}: the same ID with different labels")
        same = same or by_label.get(label_key(unit))
        if same is not None:
            if not skip_repeats:
                raise RunnerError(f"{unit['coverage_id']} repeats ledger unit {same['coverage_id']}")
            repeats.append(same["coverage_id"])
            continue
        ledger.append(unit)
        by_id[unit["coverage_id"]] = by_label[label_key(unit)] = unit
        added.append(unit["coverage_id"])
    return added, repeats


def cmd_seed(args):
    run = Run(args.run)
    specs = read_json(args.spec)
    if not isinstance(specs, list):
        raise RunnerError("the seed spec is a JSON array of units")
    ledger = run.ledger()
    units = [build_unit(run, s, i) for i, s in enumerate(specs)]
    add_units(ledger, units)
    run.meta["selected_companions"] = companion_files(ledger)
    run.save(ledger=ledger)
    run.save_meta()
    print(json.dumps({"added": len(units), "units": len(ledger),
                      "by_status": {s: sum(u["status"] == s for u in ledger) for s in sorted({u["status"] for u in ledger})}}))


def cmd_carry(args):
    run = Run(args.run)
    ledger, candidates = run.ledger(), run.candidates()
    record = next((r for r in read_json(args.prior_findings) if r.get("fingerprint") == args.fingerprint), None)
    if record is None:
        raise RunnerError(f"{args.prior_findings} has no record {args.fingerprint}")
    expected = {"confirmed": "prior_confirmed_same_source", "needs_validation": "prior_needs_validation"}.get(record["verdict"])
    if expected is None:
        raise RunnerError("only a confirmed or needs_validation record is carried; a rejected one only suppresses its claim")
    unit = next((u for u in ledger if u["coverage_id"] == args.unit), None)
    if unit is None or unit["status"] != "planned" or unit["prior_status"] != expected:
        raise RunnerError(f"carry needs a planned unit with prior_status {expected}")
    if args.fingerprint in candidates:
        raise RunnerError(f"{args.fingerprint} is already a candidate")
    errors = record_errors(record, run.branches())
    if errors:
        raise RunnerError(f"the prior record no longer meets the current schema, so it is current work, not carried: {errors[:3]}")
    draft = {k: v for k, v in record.items() if k != "verdict"}
    draft["proposed_verdict"] = record["verdict"]
    candidates[args.fingerprint] = {"state": "unvalidated", "verifiers": [],
                                    "carried": {"from": str(pathlib.Path(args.prior_findings).resolve()), "verdict": record["verdict"]},
                                    "proposals": [{"hunter": None, "units": [args.unit], "candidate": draft}]}
    run.save(candidates=candidates)
    print(json.dumps({"carried": args.fingerprint, "unit": args.unit, "verdict": record["verdict"]}))


def carried_units(candidates):
    return {cid for c in candidates.values() if c.get("carried") and c.get("state") == "unvalidated" for cid in linked_units(c)}


def cmd_assign(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    fresh_id(run, args.agent_id, calls, ledger, candidates)
    gap = budget_gap(run, calls, candidates, "hunter", hunting_wave(calls))
    if gap and (gap.startswith("a quick run") or not args.over_budget):
        raise Refused(gap + ("" if gap.startswith("a quick run") else ". Defer the unit, or pass --over-budget REASON once the user agrees."))
    by_id = {u["coverage_id"]: u for u in ledger}
    carried = carried_units(candidates)
    for cid in args.units:
        unit = by_id.get(cid)
        if unit is None or unit["status"] != "planned":
            raise RunnerError(f"{cid} is not a planned unit")
        if cid in carried:
            raise RunnerError(f"{cid} holds a carried record; its Phase 3 verifier becomes the owner, not a hunter")
        unit.update(status="in_progress", agent_id=args.agent_id)
    for sub in ("scratch", "artifacts"):
        (run.agent_dir(args.agent_id) / sub).mkdir(parents=True, exist_ok=True)
    run.save(ledger=ledger)
    print(json.dumps({"agent": args.agent_id, "units": len(args.units)}))


def cmd_unassign(args):
    run = Run(args.run)
    ledger, calls = run.ledger(), run.calls()
    if any(c["agent_id"] == args.agent_id for c in calls["calls"]):
        raise RunnerError(f"{args.agent_id} has a prompt and counts as launched; apply its reply (or its discard) instead")
    mine = [u for u in ledger if u["agent_id"] == args.agent_id and u["status"] == "in_progress"]
    for unit in mine:
        replan(unit)
    run.save(ledger=ledger)
    print(json.dumps({"agent": args.agent_id, "planned_again": len(mine)}))


def cmd_defer(args):
    run = Run(args.run)
    ledger = run.ledger()
    carried = carried_units(run.candidates())
    picked = [u for u in ledger if u["status"] == "planned" and u["coverage_id"] not in carried
              and (args.all_planned or u["coverage_id"] in args.units)]
    if not args.all_planned and len(picked) != len(set(args.units)):
        raise RunnerError("every unit to defer must be planned and hold no carried record")
    for unit in picked:
        unit.update(status="deferred", agent_id=None, reviewed_paths=[], local_checks=[], result_fingerprints=[],
                    unresolved=[args.reason])
    run.save(ledger=ledger)
    print(json.dumps({"deferred": len(picked), "reason": args.reason}))


# ---------------------------------------------------------------- prompts


def boundary_text(run, agent_id):
    scratch, artifacts = run.agent_dir(agent_id) / "scratch", run.agent_dir(agent_id) / "artifacts"
    text = (f"Agent ID: {agent_id}. Target repository root (every path is relative to it): {run.target}. "
            f"Your scratch directory, the only place you may write: {scratch}/. "
            f"Retained artifacts, parent-owned and never written by you: {artifacts}/.\n\n")
    if run.sandbox:
        return text + (f"The parent-approved OS-enforced sandbox for target-controlled code: {run.meta['sandbox']}. "
                       "Run target-controlled code only inside it. Read-only source commands need no sandbox.")
    return text + ("No OS-enforced sandbox that meets the controls above is available on this host, so you must not run "
                   "target-controlled code: no package-manager scripts, builds, tests, interpreters or browsers on target "
                   "files. Read-only commands (reading, searching and listing files, git grep) are fine. Every check is "
                   "method \"source\" with artifact null. A decisive runtime or deployment fact becomes needs_validation.")


def allowlist_text(run, entry):
    if not run.sandbox or not entry.get("allowlist"):
        return "Promotion allowlist: empty. Per-file and cumulative byte limits: 0."
    rows = "\n".join(f"- {path}: at most {size} bytes" for path, size in sorted(entry["allowlist"].items()))
    return (f"Promotion allowlist (scratch-relative path and per-file byte limit):\n{rows}\nCumulative limit: "
            f"{entry.get('total_bytes') or sum(entry['allowlist'].values())} bytes. A local check's artifact is "
            f"agents/{entry['agent_id']}/artifacts/<path> for a file on this list, which the parent promotes after "
            "your sandbox exits.")


def prior_records(run, fp):
    found = []
    for prior in run.meta.get("prior_runs", []):
        records = read_json(pathlib.Path(prior["path"]) / "findings.json", [])
        found += [dict(r, _prior_run=prior["path"]) for r in records if r.get("fingerprint") == fp]
    return found


def prior_gaps(run):
    lines = []
    for prior in run.meta.get("prior_runs", []):
        path = pathlib.Path(prior["path"])
        if not prior.get("ledger"):
            lines.append(f"- {path}: no compatible coverage-ledger.json")
            continue
        for unit in read_json(path / "coverage-ledger.json", []):
            if unit.get("status") not in ("covered", "candidate", "not_applicable"):
                reason = (unit.get("unresolved") or [""])[0]
                lines.append(f"- {unit.get('status')}: {unit.get('coverage_id')}" + (f" ({reason})" if reason else ""))
        for record in read_json(path / "findings.json", []):
            if record.get("verdict") == "needs_validation":
                lines.append(f"- needs_validation: {record.get('fingerprint')}: {record.get('title')}")
    return "\n".join(lines) if lines else "No prior ledger exists for this repository."


def carried_exclusions(ledger, candidates):
    same_source = {u["coverage_id"] for u in ledger if u.get("prior_status") == "prior_confirmed_same_source"}
    out = []
    for fp, c in sorted(candidates.items()):
        if (c.get("carried") or {}).get("verdict") == "confirmed" and same_source & set(linked_units(c)):
            draft = c["proposals"][0]["candidate"]
            out.append({"fingerprint": fp, "title": draft.get("title"), "root_cause": draft.get("root_cause")})
    return out


UNIT_FIELDS = ("coverage_id", "canonical_refs", "surface", "boundary", "subsystem", "lifecycle", "attack_class",
               "starting_paths", "ordinary_attack_class_block", "selected_companion_blocks", "excluded_blocks")


def hunter_prompt(run, entry, ledger, candidates):
    skill, agent_id = run.skill, entry["agent_id"]
    mine = [u for u in ledger if u["agent_id"] == agent_id and u["status"] == "in_progress"]
    if not mine:
        raise RunnerError(f"no in_progress units are assigned to {agent_id}; run assign first")
    peers = sorted(u["coverage_id"] for u in ledger if u["agent_id"] not in (agent_id, None))
    selected, excluded = [], {}
    for unit in mine:
        for ref in ([unit["ordinary_attack_class_block"]] if unit["ordinary_attack_class_block"] else []) + unit["selected_companion_blocks"]:
            if ref not in selected:
                selected.append(ref)
        for item in unit["excluded_blocks"]:
            excluded.setdefault(item["block"], item["reason"])
    exclusions = carried_exclusions(ledger, candidates)
    hunting = "HUNTING.md"
    units = [{k: u[k] for k in UNIT_FIELDS if k in u} for u in mine]
    parts = [
        "Your goal is to find source-grounded security invariant failures in your assigned coverage units of the target "
        "repository" + ("" if run.sandbox else ", using source review only") + ". You must return exactly one JSON "
        "object matching the structured-result contract at the end of this prompt, with no surrounding prose.",
        "# Architecture\n\n" + run.architecture(),
        f"# Assigned units\n\nTarget repository root (all paths are relative to it): {run.target}\n\n```json\n"
        + json.dumps(units, indent=2, ensure_ascii=False) + "\n```",
        "# Selected blocks (verbatim)\n\n" + "\n\n".join(f"## {r}\n\n{skill.block(r)}" for r in selected),
        "# Excluded blocks\n\n" + ("\n".join(f"- {b}: {r}" for b, r in excluded.items()) or "None."),
        "# Core hunting method\n\n```text\n" + skill.fenced_after(hunting, "#### Core hunting method") + "\n```",
        "# Promotion procedure\n\n```text\n" + skill.fenced_after(hunting, "#### Promotion procedure") + "\n```",
        "# Core validation rules\n\n```text\n" + skill.fenced_after(hunting, "#### Core validation rules") + "\n```",
        "# Exclusions\n\nCarried same-source prior confirmed exclusions (fingerprint, title, root cause):\n"
        + ("\n".join(f"- {e['fingerprint']}: {e['title']}: {e['root_cause']}" for e in exclusions) or "None.")
        + "\n\nPeer-owned coverage IDs you must not duplicate:\n" + ("\n".join(f"- {p}" for p in peers) or "None."),
        "# Execution and output\n\n" + boundary_text(run, agent_id) + "\n\n" + allowlist_text(run, entry) + "\n\n"
        + skill.between(hunting, "## Structured hunter result", "## Parent consolidation")
        + "\n\n## Schema branches (verbatim from report-schema.json)\n\n```json\n"
        + skill.branch_text("confirmed", "needs_validation") + "\n```",
    ]
    return "\n\n".join(parts)


def critic_prompt(run, entry, ledger, candidates):
    if any(u["status"] == "in_progress" for u in ledger):
        raise RunnerError("units are still in_progress; apply every hunter result (or return it to planned) first")
    if run.profile == "quick":
        kind = "final coverage critic"
    else:
        kind = "final-clean coverage critic" if entry.get("final_clean") else "post-wave coverage critic"
    scoped = ", scoped" if run.meta.get("scope_paths") else ""
    states = {fp: {"state": c["state"], "units": linked_units(c),
                   "proposed_verdict": c["proposals"][0]["candidate"].get("proposed_verdict") if c.get("proposals") else None,
                   "title": c["proposals"][0]["candidate"].get("title") if c.get("proposals") else None}
              for fp, c in sorted(candidates.items()) if c.get("state") != "merged"}
    parts = [
        f"You are the {kind} ({entry['agent_id']}) of a {run.profile}-profile{scoped} security audit. Read source, but do "
        f"not write files or run target code; read-only commands only. Target repository root: {run.target}. "
        "Return exactly the JSON object below, with no surrounding prose.",
        run.skill.between("HUNTING.md", "## Coverage-critic waves", "The parent rejects proposed units"),
        "# Architecture\n\n" + run.architecture(),
        "# Coverage ledger\n\n```json\n" + json.dumps(ledger, indent=2, ensure_ascii=False) + "\n```",
        "# Current candidate fingerprints and states\n\n```json\n" + json.dumps(states, indent=2, ensure_ascii=False) + "\n```",
        "# Prior-ledger gap summary\n\n" + prior_gaps(run),
    ]
    uncovered = read_json(run.file("uncovered.json"), [])
    parts.append("# Boundaries hunters reported as uncovered\n\n" + ("```json\n" + json.dumps(uncovered, indent=2, ensure_ascii=False)
                                                                   + "\n```" if uncovered else "None."))
    if run.meta.get("scope_paths"):
        parts.append("# Review scope\n\n" + "\n".join(f"- {p}" for p in run.meta["scope_paths"]))
    return "\n\n".join(parts)


def unit_checks(ledger, cids, hidden=()):
    """Linked units' checks, leaving out those owned by `hidden` agents (other verifiers)."""
    by_id = {u["coverage_id"]: u for u in ledger}
    out = []
    for cid in cids:
        if cid in by_id:
            checks = [k for k in by_id[cid]["local_checks"] if k["agent_id"] not in hidden]
            out.append({"coverage_id": cid, "checks": checks, "artifacts": sorted({k["artifact"] for k in checks if k.get("artifact")})})
    return out


def verifier_ids(calls):
    return {c["agent_id"] for c in calls["calls"] if c["role"] in ("verifier", "record-verifier")}


def record_checklist(run):
    return run.skill.between(VR, "For `confirmed`, require it to check:", "Each verifier returns exactly one JSON object")


def verifier_prompt(run, entry, ledger, candidates, fp):
    skill, c = run.skill, candidates[fp]
    cids = linked_units(c)
    by_id = {u["coverage_id"]: u for u in ledger}
    blocks = [f"{f}#{s}" for f in companion_files([by_id[i] for i in cids if i in by_id]) for s in ("Core discipline", "Validation rules")]
    priors = prior_records(run, fp)
    parts = [
        skill.fenced_after(VR, "#### Candidate-verifier prompt"),
        "# Promotion procedure\n\n```text\n" + skill.fenced_after(VR, "Copy this promotion procedure verbatim") + "\n```",
        "# Execution boundary\n\n" + boundary_text(run, entry["agent_id"]) + "\n\n" + allowlist_text(run, entry),
    ]
    if run.profile == "quick":
        parts.append("# Final record verification\n\nThis is a quick-profile run, so Phase 3 and Phase 5 merge: you also "
                     "perform the final record verification below, and your reply carries the final schema-shaped "
                     "record.\n\n" + record_checklist(run))
    parts += [
        "# Architecture facts\n\n" + run.architecture(),
        "# Relevant validation blocks (verbatim)\n\n" + ("\n\n".join(f"## {r}\n\n{skill.block(r)}" for r in blocks)
                                                          or "None: no companion file is selected for the linked units."),
        "# Linked coverage-unit checks\n\n```json\n" + json.dumps(unit_checks(ledger, cids, entry["hidden"]), indent=2, ensure_ascii=False) + "\n```",
        "# Candidate (proposed_verdict is the hunter's, not a conclusion)\n\n" + "\n\n".join(
            f"Proposal {i + 1}{' (the strongest trace)' if i == 0 else ''}:\n\n```json\n"
            + json.dumps(p["candidate"], indent=2, ensure_ascii=False) + "\n```" for i, p in enumerate(c["proposals"])),
    ]
    parts += [
        "# Prior records with the same fingerprint\n\n" + ("```json\n" + json.dumps(priors, indent=2, ensure_ascii=False) + "\n```" if priors else "None."),
        "# Schema branches (verbatim from report-schema.json)\n\n```json\n" + skill.branch_text(*VERDICTS) + "\n```",
    ]
    return "\n\n".join(parts)


def record_verifier_prompt(run, entry, ledger, candidates, fp):
    skill, c = run.skill, candidates[fp]
    pending = c.get("pending_replacement")
    record = pending["record"] if pending else c["final_record"]
    intro = (f"You are a fresh Phase 5 record verifier ({entry['agent_id']}). You did not hunt, validate or rewrite this "
             "record. Check the structured record below against the current repository source, inside the execution "
             "boundary, and return exactly one JSON object with no surrounding prose.")
    if pending:
        intro += (" This record is a proposed replacement of an earlier final record. Recheck the current source and "
                  "independently reproduce any decisive local result, then return verified or another replacement.")
    parts = [
        intro,
        skill.between(VR, "For `confirmed`, require it to check:", "Do not apply a Phase 5 replacement"),
        "# Execution boundary\n\n" + boundary_text(run, entry["agent_id"]) + "\n\n" + allowlist_text(run, entry),
    ]
    if run.sandbox:
        parts.append("# Promotion procedure\n\n```text\n" + skill.fenced_after(VR, "Copy this promotion procedure verbatim") + "\n```")
    priors = prior_records(run, fp)
    parts += [
        "# Architecture facts\n\n" + run.architecture(),
        "# Linked coverage-unit checks\n\n```json\n" + json.dumps(unit_checks(ledger, linked_units(c), entry["hidden"]), indent=2, ensure_ascii=False) + "\n```",
        "# Record to verify\n\n```json\n" + json.dumps(record, indent=2, ensure_ascii=False) + "\n```",
        "# Prior records with the same fingerprint\n\n" + ("```json\n" + json.dumps(priors, indent=2, ensure_ascii=False) + "\n```" if priors else "None."),
        "# Schema branches (verbatim from report-schema.json)\n\n```json\n" + skill.branch_text(*VERDICTS) + "\n```",
    ]
    return "\n\n".join(parts)


def parse_allowlist(run, items, total):
    if not items:
        return {}
    if not run.sandbox:
        raise RunnerError("a source-only run promotes nothing, so it takes no --allow-artifact")
    allow = {}
    for item in items:
        path, _, size = item.rpartition("=")
        if not safe_path(path) or not size.isdigit() or int(size) <= 0:
            raise RunnerError(f"--allow-artifact takes RELATIVE/PATH=BYTES, got {item!r}")
        allow[path] = int(size)
    if total is not None and total <= 0:
        raise RunnerError("--total-bytes must be positive")
    return allow


def cmd_prompt(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    role, agent_id = args.role, args.agent_id
    existing = next((c for c in calls["calls"] if c["agent_id"] == agent_id), None)
    if existing is None:
        if role == "hunter":
            if not any(u["agent_id"] == agent_id and u["status"] == "in_progress" for u in ledger):
                raise RunnerError(f"no in_progress units are assigned to {agent_id}; run assign first")
        else:
            fresh_id(run, agent_id, calls, ledger, candidates)
    fields = {}
    if role in ("hunter", "verifier", "record-verifier"):
        fields["allowlist"] = parse_allowlist(run, args.allow_artifact, args.total_bytes)
        if args.total_bytes:
            fields["total_bytes"] = args.total_bytes
    if role == "hunter":
        fields["wave"] = existing.get("wave") if existing else hunting_wave(calls)
    elif role == "critic":
        fields["wave"] = max([c.get("wave", 1) for c in calls["calls"] if c["role"] == "hunter"] or [1])
        fields["final_clean"] = bool(args.final_clean) and run.profile != "quick"
        if args.final_clean and run.profile == "quick":
            raise RunnerError("a quick run has exactly one final critic; drop --final-clean")
    elif role in ("verifier", "record-verifier"):
        if not args.fingerprint:
            raise RunnerError(f"a {role} prompt needs --fingerprint")
        fp = args.fingerprint
        if fp not in candidates:
            raise RunnerError(f"{fp} is not a candidate")
        c = candidates[fp]
        involved = {p["hunter"] for p in c["proposals"]} | set(c.get("verifiers", [])) | set(c.get("record_verifiers", []))
        if role == "verifier":
            if c["state"] != "unvalidated":
                raise RunnerError(f"{fp} is {c['state']}; only an unvalidated candidate gets a Phase 3 verifier")
            if not any(x["role"] == "critic" for x in calls["calls"]):
                note("no coverage critic has run yet; Phase 3 starts after the critic pass or a recorded early stop")
        else:
            if run.profile == "quick":
                raise RunnerError("a quick run merges Phase 5 into the Phase 3 verifier")
            if c["state"] not in ("confirmed", "needs_validation") or c.get("phase5") == "verified":
                raise RunnerError(f"{fp} needs no Phase 5 verifier (state {c['state']}, phase5 {c.get('phase5')})")
        if agent_id in involved and (existing is None or existing["role"] != role):
            raise RunnerError(f"{agent_id} already worked on {fp}; a verifier must be independent")
        fields["fingerprint"] = fp
    elif role == "recon":
        if not args.recon_agent and not args.text_file:
            raise RunnerError("a recon prompt needs --agent 1a|1b|1c|1d, or --text-file for a focused agent")
    entry = register(run, calls, candidates, agent_id, role, over_budget=args.over_budget, **fields)
    entry_hidden = sorted(verifier_ids(calls) - {agent_id})
    entry = dict(entry, hidden=entry_hidden)
    if role == "recon":
        if args.text_file:
            text = pathlib.Path(args.text_file).read_text(encoding="utf-8")
        else:
            text = (f"Target repository root: {run.target}. Use read-only commands only: do not write files and do not "
                    "run target-controlled code.\n\n"
                    + run.skill.fenced_after("RECONNAISSANCE.md", f"**Agent {args.recon_agent}").replace("<target>", run.target))
    elif role == "hunter":
        text = hunter_prompt(run, entry, ledger, candidates)
    elif role == "critic":
        text = critic_prompt(run, entry, ledger, candidates)
    elif role == "verifier":
        text = verifier_prompt(run, entry, ledger, candidates, fields["fingerprint"])
    else:
        text = record_verifier_prompt(run, entry, ledger, candidates, fields["fingerprint"])
    if role in ("verifier", "record-verifier"):
        c = candidates[fields["fingerprint"]]
        key = "verifiers" if role == "verifier" else "record_verifiers"
        if agent_id not in c.setdefault(key, []):
            c[key].append(agent_id)
        c["verifier" if role == "verifier" else "record_verifier"] = agent_id
    folder = run.agent_dir(agent_id)
    for sub in ("scratch", "artifacts"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    path = folder / "prompt.md"
    path.write_text(text + "\n", encoding="utf-8")
    run.save(candidates=candidates, calls=calls)
    print(json.dumps({"prompt": str(path), "bytes": len(text.encode("utf-8")), "model": entry["model"],
                      "agent_prompt": f"Read {path} and follow it exactly. Your final message is only what it asks you to return."}, indent=1))


# ---------------------------------------------------------------- replies


def final_message(transcript):
    """The SubagentHandback message, or else the last assistant text, from a JSONL transcript."""
    final = None
    with open(transcript, encoding="utf-8") as lines:
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("content"), list):
                continue
            for part in message["content"]:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and part.get("name") == "SubagentHandback":
                    final = (part.get("input") or {}).get("message")
                elif part.get("type") == "text" and str(part.get("text", "")).strip():
                    final = part["text"]
    return final


def node_record_errors(run, agent_id, record):
    check = run.agent_dir(agent_id) / "record-check.json"
    write_json(check, [record])
    try:
        result = run.validator("validate-findings.cjs", check)
    finally:
        check.unlink()
    return [] if result is None or result[0] == 0 else [f"validate-findings.cjs: {result[1]}"]


def reply_errors(run, role, agent_id, reply):
    if role == "hunter":
        errors = shape_errors(reply, {"units": list, "candidates": list, "hardening": list, "uncovered": list}, "reply")
        if not errors:
            errors += [f"reply.units[{i}]: expected an object" for i, u in enumerate(reply["units"]) if not isinstance(u, dict)]
            errors += [f"reply.candidates[{i}]: expected an object" for i, c in enumerate(reply["candidates"]) if not isinstance(c, dict)]
            errors += [f"reply.uncovered[{i}]: expected an object" for i, c in enumerate(reply["uncovered"]) if not isinstance(c, dict)]
            errors += strings_errors(reply["hardening"], "reply.hardening")
        return errors
    if role == "critic":
        errors = shape_errors(reply, {"missing_units": list, "reassign_ids": list, "resolved_prior_leads": list, "stop": bool}, "reply")
        if errors:
            return errors
        unit_fields = {"surface": str, "boundary": str, "subsystem": str, "attack_class": str, "starting_paths": list,
                       "selected_companion_blocks": list, "excluded_blocks": list, "reason": str}
        for i, unit in enumerate(reply["missing_units"]):
            where = f"reply.missing_units[{i}]"
            found = shape_errors(unit, unit_fields, where)
            if not found:
                found += strings_errors(unit["starting_paths"], where + ".starting_paths")
                found += strings_errors(unit["selected_companion_blocks"], where + ".selected_companion_blocks")
                for j, item in enumerate(unit["excluded_blocks"]):
                    found += shape_errors(item, {"block": str, "reason": str}, f"{where}.excluded_blocks[{j}]")
            errors += found
        return errors + strings_errors(reply["reassign_ids"], "reply.reassign_ids") + strings_errors(reply["resolved_prior_leads"], "reply.resolved_prior_leads")
    if role == "verifier":
        errors = shape_errors(reply, {"decision": str, "record": dict}, "reply")
        if errors:
            return errors
        if reply["decision"] not in VERDICTS:
            return [f"reply.decision: {reply['decision']!r} is not one of {list(VERDICTS)}"]
        if reply["record"].get("verdict") != reply["decision"]:
            return [f"reply.record.verdict {reply['record'].get('verdict')!r} does not match decision {reply['decision']!r}"]
    else:
        decision = reply.get("decision") if isinstance(reply, dict) else None
        if decision == "verified":
            return shape_errors(reply, {"decision": str, "fingerprint": str}, "reply")
        if decision != "replace":
            return [f"reply.decision: expected verified or replace, got {decision!r}"]
        errors = shape_errors(reply, {"decision": str, "reason": str, "record": dict}, "reply")
        if errors:
            return errors
    errors = record_errors(reply["record"], run.branches())
    return errors or node_record_errors(run, agent_id, reply["record"])


def cmd_extract(args):
    run = Run(args.run)
    calls = run.calls()
    entry = run.call(calls, args.agent_id)
    if entry["status"] != "built":
        raise RunnerError(f"{args.agent_id} already has status {entry['status']}; a re-run gets a fresh ID")
    folder = run.agent_dir(args.agent_id)
    text = final_message(pathlib.Path(args.transcript).expanduser())
    if entry["role"] == "recon":
        if text is None:
            raise RunnerError("the transcript has no final message")
        (folder / "result.md").write_text(text.strip() + "\n", encoding="utf-8")
        entry["status"] = "returned"
        run.save(calls=calls)
        print(json.dumps({"saved": str(folder / "result.md"), "chars": len(text)}))
        return 0
    errors, reply = [], None
    if text is None:
        errors = ["the transcript has no final message"]
    else:
        try:
            reply = json.loads(text.strip())
        except json.JSONDecodeError as error:
            errors = [f"not one JSON object: {error}"]
        if not errors:
            errors = reply_errors(run, entry["role"], args.agent_id, reply)
    if errors:
        (folder / "discarded-reply.txt").write_text(text or "", encoding="utf-8")
        result = folder / "result.json"
        if result.exists():
            result.unlink()
        entry.update(status="discarded", problems=errors[:20])
        run.save(calls=calls)
        print(json.dumps({"discarded": args.agent_id, "errors": errors[:10], "raw": str(folder / "discarded-reply.txt"),
                          "next": "do not repair it; re-run with a fresh agent if the budget allows"}, indent=1))
        return 1
    write_json(folder / "result.json", reply)
    entry["status"] = "returned"
    run.save(calls=calls)
    print(json.dumps({"saved": str(folder / "result.json"), "role": entry["role"]}))
    return 0


# ---------------------------------------------------------------- applying replies


def promoted(calls, owner):
    return {p["artifact"] for c in calls["calls"] if c["agent_id"] == owner for p in c.get("promoted", [])}


def accept_check(run, calls, check):
    if not isinstance(check, dict):
        raise ValueError("a check is not an object")
    owner = check.get("agent_id")
    if not safe_agent_id(owner):
        raise ValueError(f"unsafe check agent_id {owner!r}")
    paths = check.get("reviewed_paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("a check needs a nonempty reviewed_paths list")
    bad = [p for p in paths if not safe_path(p)]
    if bad:
        raise ValueError(f"reviewed path {bad[0]!r} is not repository-relative")
    if not ledger_text(check.get("invariant")) or not ledger_text(check.get("result")):
        raise ValueError("a check's invariant and result must be trimmed visible text of at most 4096 characters")
    method, artifact = check.get("method"), check.get("artifact")
    if method == "source":
        if artifact is not None:
            raise ValueError("a source check uses artifact null")
    elif method == "local":
        if not run.sandbox:
            raise ValueError("a source-only run accepts no local check")
        if artifact not in promoted(calls, owner):
            raise ValueError(f"artifact {artifact!r} was not promoted into agents/{owner}/artifacts/ by the parent")
        info = os.lstat(run.root / artifact)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"artifact {artifact!r} is no longer a single-link regular file")
    else:
        raise ValueError(f"check method {method!r} is not source or local")
    return {"agent_id": owner, "reviewed_paths": sorted(set(paths)), "invariant": check["invariant"], "method": method,
            "result": check["result"], "artifact": artifact}


def accept_unit(run, calls, result):
    """The ledger fields for one unit result, checked as HUNTING.md's parent consolidation requires; nothing is rewritten."""
    disposition = result.get("disposition")
    if disposition not in CLOSED:
        raise ValueError(f"disposition {disposition!r} is not covered, candidate or blocked")
    lists = {k: result.get(k) for k in ("checks", "reviewed_paths", "candidate_fingerprints", "unresolved")}
    wrong = [k for k, v in lists.items() if not isinstance(v, list)]
    if wrong:
        raise ValueError(f"{wrong[0]} must be a list")
    checks = [accept_check(run, calls, c) for c in lists["checks"]]
    fps, unresolved = lists["candidate_fingerprints"], lists["unresolved"]
    if not checks:
        raise ValueError("no checks")
    bad = [f for f in fps if not isinstance(f, str) or not FINGERPRINT.match(f)]
    if bad:
        raise ValueError(f"bad fingerprint {bad[0]!r}")
    if not all(ledger_text(u) for u in unresolved):
        raise ValueError("unresolved entries must be trimmed visible text of at most 4096 characters")
    if disposition == "covered" and (fps or unresolved):
        raise ValueError("a covered unit carries no fingerprint or unresolved fact")
    if disposition == "candidate" and not fps:
        raise ValueError("a candidate unit needs a fingerprint")
    if disposition == "blocked" and (fps or not unresolved):
        raise ValueError("a blocked unit needs unresolved facts and no fingerprint")
    union = sorted({p for c in checks for p in c["reviewed_paths"]})
    if sorted(set(lists["reviewed_paths"])) != union:
        raise ValueError("the unit's reviewed_paths are not exactly the union of its checks' paths")
    return dict(status=disposition, local_checks=checks, reviewed_paths=union, result_fingerprints=sorted(set(fps)),
                unresolved=list(unresolved))


def replan(unit):
    unit.update(status="planned", agent_id=None, reviewed_paths=[], local_checks=[], result_fingerprints=[], unresolved=[])


def cmd_apply_hunter(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    entry = run.call(calls, args.agent_id, "hunter")
    if entry["status"] not in ("returned", "discarded"):
        raise RunnerError(f"{args.agent_id} has status {entry['status']}; run extract on its transcript first")
    mine = {u["coverage_id"]: u for u in ledger if u["agent_id"] == args.agent_id and u["status"] == "in_progress"}
    problems, closed = [], {}
    reply = read_json(run.agent_dir(args.agent_id) / "result.json", None) if entry["status"] == "returned" else None
    if reply is None:
        problems.append("the reply was discarded; every unit goes back to planned")
        reply = {"units": [], "candidates": [], "hardening": [], "uncovered": []}
    ids = [r.get("coverage_id") for r in reply["units"]]
    for result in reply["units"]:
        cid = result.get("coverage_id")
        if cid not in mine:
            problems.append(f"unknown unit {cid!r}")
        elif ids.count(cid) > 1:
            if cid not in closed:
                problems.append(f"{cid}: listed {ids.count(cid)} times; back to planned")
            closed[cid] = None
        else:
            try:
                closed[cid] = accept_unit(run, calls, result)
            except (ValueError, TypeError, KeyError, OSError) as error:
                problems.append(f"{cid}: {error}; back to planned")
    closed = {cid: fields for cid, fields in closed.items() if fields}
    excluded = {e["fingerprint"] for e in carried_exclusions(ledger, candidates)}
    branches, accepted = run.branches(), []
    for candidate in reply["candidates"]:
        fp = candidate.get("fingerprint")
        if not isinstance(fp, str) or not FINGERPRINT.match(fp):
            problems.append(f"candidate with bad fingerprint {fp!r} dropped")
            continue
        if fp in excluded:
            problems.append(f"{fp}: re-reports a carried same-source confirmed root cause; dropped")
            continue
        errors = draft_errors(candidate, branches)
        if not errors:
            draft = {k: v for k, v in candidate.items() if k != "proposed_verdict"}
            errors = node_record_errors(run, args.agent_id, dict(draft, verdict=candidate["proposed_verdict"]))
        if errors:
            problems.append(f"{fp}: fails its schema branch, dropped: {'; '.join(errors[:3])}")
            continue
        if candidate["proposed_verdict"] not in ("confirmed", "needs_validation"):
            problems.append(f"{fp}: proposed_verdict must be confirmed or needs_validation; dropped")
            continue
        accepted.append(candidate)
    proposed = {c["fingerprint"] for c in accepted}
    for cid in list(closed):
        orphans = [fp for fp in closed[cid]["result_fingerprints"]
                   if fp not in proposed and resolve_fp(candidates, fp) not in candidates]
        if orphans:
            problems.append(f"{cid}: fingerprint {orphans[0]} has no valid candidate; back to planned")
            del closed[cid]
    for cid, unit in mine.items():
        if cid in closed:
            unit.update(closed[cid], hardening_notes=list(reply["hardening"]))
        else:
            if cid not in ids and reply["units"]:
                problems.append(f"{cid}: missing from the reply; back to planned")
            replan(unit)
    summary = []
    for candidate in accepted:
        fp = candidate["fingerprint"]
        units = sorted(cid for cid in closed if fp in closed[cid]["result_fingerprints"])
        if not units:
            problems.append(f"{fp}: no accepted unit links it; dropped")
            continue
        key = resolve_fp(candidates, fp)
        slot = candidates.setdefault(key, {"state": "unvalidated", "proposals": [], "verifiers": []})
        if slot["state"] != "unvalidated":
            problems.append(f"{fp}: already {slot['state']}; kept as an extra proposal only")
        slot["proposals"].append({"hunter": args.agent_id, "units": units, "candidate": candidate})
        summary.append({"fingerprint": key, "proposed_verdict": candidate["proposed_verdict"]})
    for cid, fields in closed.items():
        for fp in fields["result_fingerprints"]:
            if fp not in proposed:
                slot = candidates[resolve_fp(candidates, fp)]
                slot["linked_units"] = sorted(set(slot.get("linked_units", [])) | {cid})
    uncovered = read_json(run.file("uncovered.json"), [])
    uncovered += [dict(item, reported_by=args.agent_id) for item in reply["uncovered"]]
    entry["status"] = "applied"
    try:
        run.save(ledger=ledger, candidates=candidates, calls=calls, extra=[("uncovered.json", uncovered)])
    except Refused as error:
        problems.append(f"the ledger validator refused the result, so every unit goes back to planned: {error}")
        for unit in mine.values():
            replan(unit)
        summary = []
        run.save(ledger=ledger, calls=calls)
    print(json.dumps({"agent": args.agent_id, "units": {cid: u["status"] for cid, u in sorted(mine.items())},
                      "candidates": summary, "uncovered": len(reply["uncovered"]), "problems": problems}, indent=1))
    return 1 if problems else 0


def cmd_merge(args):
    run = Run(args.run)
    ledger, candidates = run.ledger(), run.candidates()
    keep = args.keep
    names = [keep] + args.drop
    for fp in names:
        if fp not in candidates or candidates[fp]["state"] != "unvalidated":
            raise RunnerError(f"{fp} is not an unvalidated candidate")
    if len(set(names)) != len(names):
        raise RunnerError("merge needs distinct fingerprints")
    lead = args.lead or keep
    if lead not in names:
        raise RunnerError("--lead must be one of the merged fingerprints")
    proposals = [dict(p, merged_from=fp) if fp != keep else p for fp in names for p in candidates[fp]["proposals"]]
    proposals.sort(key=lambda p: 0 if p.get("merged_from", keep) == lead else 1)
    candidates[keep]["proposals"] = proposals
    for fp in args.drop:
        candidates[fp] = {"state": "merged", "merged_into": keep, "reason": args.reason}
    for unit in ledger:
        hit = [fp for fp in unit["result_fingerprints"] if fp in args.drop]
        if hit:
            unit["result_fingerprints"] = sorted({keep if fp in args.drop else fp for fp in unit["result_fingerprints"]})
            unit["duplicate_fingerprints"] = sorted(set(unit.get("duplicate_fingerprints", [])) | set(hit))
    run.save(ledger=ledger, candidates=candidates)
    print(json.dumps({"kept": keep, "merged": args.drop, "proposals": len(proposals)}))


def parse_numbered(items, flag):
    out = {}
    for item in items or []:
        key, sep, reason = item.partition("=")
        if not sep or not visible(reason):
            raise RunnerError(f"{flag} takes KEY=REASON, got {item!r}")
        out[key] = reason
    return out


def in_scope(run, paths):
    scope = run.meta.get("scope_paths") or []
    return not scope or any(p == s.rstrip("/") or p.startswith(s.rstrip("/") + "/") for p in paths for s in scope)


def critic_unit(run, item, refs):
    """A seed spec for one accepted missing unit; refs come from --refs or are derived from the critic's labels."""
    classes = [r for r in item["selected_companion_blocks"] if r.partition("#")[2] not in COMPANION_SECTIONS]
    ordinary = f"ATTACK-CLASSES.md#{item['attack_class']}"
    try:
        run.skill.block(ordinary)
    except RunnerError:
        ordinary = None
    if refs is None:
        head = item["starting_paths"][0] if item["starting_paths"] else None
        attack = ordinary or (classes[0] if classes else None)
        if not head or not attack:
            raise RunnerError(f"cannot derive canonical refs for {item['surface']!r}; pass them with --refs")
        refs = {"surface": f"{head}#{item['surface']}", "boundary": f"{head}#{item['boundary']}",
                "subsystem": QUICK_SUBSYSTEM if run.profile == "quick" else item["subsystem"], "attack_class": attack}
    return {"surface": item["surface"], "boundary": item["boundary"], "subsystem": item["subsystem"],
            "attack_class": item["attack_class"], "refs": refs, "starting_paths": item["starting_paths"],
            "ordinary_block": ordinary, "companion_blocks": item["selected_companion_blocks"],
            "excluded_blocks": item["excluded_blocks"]}


def cmd_apply_critic(args):
    run = Run(args.run)
    ledger, calls = run.ledger(), run.calls()
    entry = run.call(calls, args.agent_id, "critic")
    if entry["status"] != "returned":
        raise RunnerError(f"{args.agent_id} has status {entry['status']}; a discarded critic is re-run with a fresh ID")
    reply = read_json(run.agent_dir(args.agent_id) / "result.json")
    refs = read_json(args.refs) if args.refs else {}
    out_of_scope, rejected = parse_numbered(args.out_of_scope, "--out-of-scope"), parse_numbered(args.reject, "--reject")
    keep_closed, reasons = parse_numbered(args.keep_closed, "--keep-closed"), parse_numbered(args.reassign_reason, "--reassign-reason")
    quick = run.profile == "quick"
    log = {"critic": args.agent_id, "final_clean": entry.get("final_clean", False), "stop": reply["stop"],
           "accepted": [], "out_of_scope": [], "rejected": [], "reassigned": [], "kept_closed": [], "resolved_prior_leads": []}
    new_units = []
    for number, item in enumerate(reply["missing_units"], 1):
        key = str(number)
        if key in rejected:
            log["rejected"].append({"unit": item, "reason": rejected[key]})
            continue
        spec = critic_unit(run, item, refs.get(key))
        if key in out_of_scope or not in_scope(run, item["starting_paths"]):
            spec.update(status="out_of_scope", reason=out_of_scope.get(key) or f"Outside the review scope: {item['reason']}")
        elif quick:
            spec.update(status="deferred", reason=f"quick_profile_final_critic: {item['reason']}")
        unit = build_unit(run, spec, number - 1)
        unit["critic"] = {"agent_id": args.agent_id, "reason": item["reason"], "derived_refs": key not in refs}
        new_units.append(unit)
    added, repeats = add_units(ledger, new_units, skip_repeats=True)
    log["repeats"] = repeats
    for unit in new_units:
        if unit["coverage_id"] in added:
            log["out_of_scope" if unit["status"] == "out_of_scope" else "accepted"].append(unit["coverage_id"])
    by_id = {u["coverage_id"]: u for u in ledger}
    for cid in reply["reassign_ids"]:
        unit = by_id.get(cid)
        if cid in keep_closed:
            log["kept_closed"].append({"coverage_id": cid, "reason": keep_closed[cid]})
            continue
        if unit is None or unit["status"] not in CLOSED:
            log["rejected"].append({"reassign_id": cid, "reason": "not a closed unit with evidence"})
            continue
        reason = reasons.get(cid, f"Reopened by coverage critic {args.agent_id}.")
        unit["attempts"].append({k: unit[k] for k in ("wave", "status", "agent_id", "reviewed_paths", "local_checks",
                                                       "result_fingerprints", "unresolved")} | {"reassignment_reason": reason})
        unit["wave"] += 1
        replan(unit)
        if quick:
            unit.update(status="deferred", unresolved=[f"quick_profile_final_critic: {reason}"])
        log["reassigned"].append(cid)
    for fp in reply["resolved_prior_leads"]:
        hits = [u for u in ledger if fp in u["result_fingerprints"] or fp in u.get("duplicate_fingerprints", [])]
        for unit in hits:
            unit.setdefault("resolved_prior_leads", []).append({"fingerprint": fp, "critic": args.agent_id})
        log["resolved_prior_leads"].append({"fingerprint": fp, "units": [u["coverage_id"] for u in hits]})
    run.meta["selected_companions"] = companion_files(ledger)
    critic_log = read_json(run.file("critic-log.json"), []) + [log]
    entry["status"] = "applied"
    run.save(ledger=ledger, calls=calls, extra=[("critic-log.json", critic_log)])
    run.save_meta()
    derived = [u["coverage_id"] for u in new_units if u["critic"]["derived_refs"] and u["coverage_id"] in added]
    clean = not log["accepted"] and not log["reassigned"]
    print(json.dumps({"accepted": log["accepted"], "out_of_scope": log["out_of_scope"], "reassigned": log["reassigned"],
                      "rejected": len(log["rejected"]), "already_in_ledger": repeats, "derived_refs": derived, "clean": clean,
                      "planned_now": sum(u["status"] == "planned" for u in ledger)}, indent=1))


def verifier_check(run, unit, agent_id, fp, decision, record):
    paths = sorted({e["file"] for k in ("trace", "evidence") for e in record.get(k, []) if isinstance(e, dict) and e.get("file")})
    unit["local_checks"] = [k for k in unit["local_checks"] if k["agent_id"] != agent_id] + [{
        "agent_id": agent_id, "reviewed_paths": paths, "invariant": f"Independent validation of candidate {fp}",
        "method": "source", "result": f"Verifier decision: {decision}.", "artifact": None}]
    unit["reviewed_paths"] = sorted({p for k in unit["local_checks"] for p in k["reviewed_paths"]})


def new_candidate(ledger, candidates, record, proposer, cids):
    """A verifier's record that names a different root cause: a new unvalidated candidate on the same units."""
    fp = record["fingerprint"]
    if fp in candidates:
        raise RunnerError(f"the reply names fingerprint {fp}, which is already a candidate; decide whether to merge, then re-run")
    draft = dict({k: v for k, v in record.items() if k != "verdict"}, proposed_verdict=record["verdict"])
    candidates[fp] = {"state": "unvalidated", "verifiers": [], "proposals": [{"hunter": proposer, "units": cids, "candidate": draft}]}
    for unit in ledger:
        if unit["coverage_id"] in cids:
            unit["result_fingerprints"] = sorted(set(unit["result_fingerprints"]) | {fp})


def cmd_apply_verifier(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    entry = run.call(calls, args.agent_id, "verifier")
    fp = entry["fingerprint"]
    c = candidates[fp]
    if entry["status"] == "built":
        raise RunnerError(f"{args.agent_id} has status built; run extract on its transcript first")
    if entry["status"] == "applied":
        raise RunnerError(f"{args.agent_id} was already applied")
    reply = read_json(run.agent_dir(args.agent_id) / "result.json", None) if entry["status"] == "returned" else None
    if reply is None:
        c["verifier_problems"] = c.get("verifier_problems", []) + [{"verifier": args.agent_id, "problem": "reply discarded"}]
        entry["status"] = "applied"
        run.save(candidates=candidates, calls=calls)
        print(json.dumps({"fingerprint": fp, "state": c["state"], "problem": "the reply was discarded",
                          "next": "re-run with a fresh verifier if the budget allows; otherwise the run ends incomplete "
                                  "with validation_budget_exhausted"}, indent=1))
        return 1
    decision, record = reply["decision"], reply["record"]
    by_id = {u["coverage_id"]: u for u in ledger}
    if record["fingerprint"] != fp:
        if not args.different_root_cause:
            print(json.dumps({"fingerprint": fp, "returned": record["fingerprint"],
                              "next": "the same root cause keeps its fingerprint. If the verifier found a different root "
                                      "cause, pass --different-root-cause: its record becomes a new candidate that needs "
                                      "its own verifier, and this one still needs a verifier too"}, indent=1))
            return 1
        cids = [cid for cid in linked_units(c) if by_id.get(cid, {}).get("status") == "candidate"]
        new_candidate(ledger, candidates, record, args.agent_id, cids)
        entry["status"] = "applied"
        run.save(ledger=ledger, candidates=candidates, calls=calls)
        print(json.dumps({"fingerprint": fp, "state": c["state"], "new_candidate": record["fingerprint"]}, indent=1))
        return 0
    c.update(state=decision, final_record=record, decided_by=args.agent_id)
    if run.profile != "quick" and decision != "rejected":
        c["phase5"] = "pending"
    for cid in linked_units(c):
        unit = by_id.get(cid)
        if unit is None:
            continue
        if unit["status"] == "planned" and c.get("carried"):
            unit.update(status="candidate", agent_id=args.agent_id, result_fingerprints=[fp], local_checks=[], unresolved=[])
        if unit["status"] == "candidate":
            verifier_check(run, unit, args.agent_id, fp, decision, record)
        unit.setdefault("verifier_decisions", {})[fp] = decision
    entry["status"] = "applied"
    run.save(ledger=ledger, candidates=candidates, calls=calls, findings=True)
    print(json.dumps({"fingerprint": fp, "decision": decision, "units": linked_units(c),
                      "phase5": c.get("phase5", "merged into phase 3" if run.profile == "quick" else None)}))
    return 0


def material_change(old, new):
    """True when a Phase 5 replacement needs a fresh verifier before it applies."""
    if VERDICT_RANK[new["verdict"]] > VERDICT_RANK[old["verdict"]]:
        return True
    if (old.get("root_cause") or old.get("claimed_root_cause")) != (new.get("root_cause") or new.get("claimed_root_cause")):
        return True
    shape = [[(e.get("kind"), e.get("file"), e.get("scope")) for e in r.get("trace", [])] for r in (old, new)]
    run_of = [((r.get("execution") or {}).get("payloads"), (r.get("execution") or {}).get("observed_result")) for r in (old, new)]
    return shape[0] != shape[1] or run_of[0] != run_of[1] or old.get("severity") != new.get("severity")


def cmd_apply_record_verifier(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    entry = run.call(calls, args.agent_id, "record-verifier")
    fp = entry["fingerprint"]
    c = candidates[fp]
    if entry["status"] in ("built", "applied"):
        raise RunnerError(f"{args.agent_id} has status {entry['status']}; it needs extract first, or was applied")
    reply = read_json(run.agent_dir(args.agent_id) / "result.json", None) if entry["status"] == "returned" else None
    problem = None if reply is not None else "the reply was discarded"
    if reply is not None and reply["decision"] == "verified" and reply["fingerprint"] != fp:
        problem = f"verified names fingerprint {reply['fingerprint']}, not {fp}"
    if problem:
        entry.update(status="applied", problems=[problem])
        run.save(calls=calls)
        print(json.dumps({"fingerprint": fp, "problem": problem, "next": "re-run with a fresh record verifier if the budget allows"}, indent=1))
        return 1
    outcome = {"fingerprint": fp, "decision": reply["decision"]}
    pending = c.get("pending_replacement")
    if reply["decision"] == "verified":
        if pending:
            c.update(final_record=pending["record"], state=pending["record"]["verdict"])
            c.pop("pending_replacement")
            outcome["applied"] = "the pending replacement"
        c["phase5"] = "verified"
    else:
        record = reply["record"]
        base = pending["record"] if pending else c["final_record"]
        if record["fingerprint"] != fp:
            cids = [u["coverage_id"] for u in ledger if u["coverage_id"] in linked_units(c) and u["status"] == "candidate"]
            new_candidate(ledger, candidates, record, args.agent_id, cids)
            outcome["new_candidate"] = record["fingerprint"]
            outcome["next"] = f"a separate root cause: run a Phase 3 verifier on it; {fp} still needs its own Phase 5 pass"
        elif material_change(base, record):
            c["pending_replacement"] = {"record": record, "proposed_by": args.agent_id, "reason": reply["reason"]}
            c["phase5"] = "replacement_pending"
            outcome["next"] = "material replacement: verify it with a fresh record verifier before it applies"
        else:
            c.update(final_record=record, state=record["verdict"], phase5="verified")
            c.pop("pending_replacement", None)
            outcome["applied"] = "non-material correction"
    for unit in ledger:
        if unit["coverage_id"] in linked_units(c):
            unit.setdefault("verifier_decisions", {})[fp] = c["state"]
    entry["status"] = "applied"
    run.save(ledger=ledger, candidates=candidates, calls=calls, findings=True)
    print(json.dumps(outcome, indent=1))
    return 0


# ---------------------------------------------------------------- artifact promotion


def _open_dir(name, dir_fd):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def promote_file(root, agent_id, rel, limit, budget_left):
    """Copy agents/<id>/scratch/<rel> to agents/<id>/artifacts/<rel> with no-follow, descriptor-relative steps.

    Returns the byte count. Raises RunnerError on any failed check; the scratch entry is then not evidence.
    """
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise RunnerError("this platform lacks descriptor-relative no-follow opens, so promotion is unavailable")
    parts = rel.split("/")
    if not safe_path(rel):
        raise RunnerError(f"{rel!r} is not a safe scratch-relative path")
    fds, created = [], None
    try:
        base = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
        fds.append(base)
        agents = _open_dir("agents", base)
        fds.append(agents)
        agent = _open_dir(agent_id, agents)
        fds.append(agent)
        fd = _open_dir("scratch", agent)
        fds.append(fd)
        for part in parts[:-1]:
            fd = _open_dir(part, fd)
            fds.append(fd)
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        fds.append(leaf)
        before = os.fstat(leaf)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RunnerError(f"{rel} is not a single-link regular file")
        if before.st_size > limit or before.st_size > budget_left:
            raise RunnerError(f"{rel} is {before.st_size} bytes, over its limit")
        chunks, size = [], 0
        while size <= before.st_size:
            chunk = os.read(leaf, min(65536, before.st_size + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(leaf)
        same = (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size)
        if size != before.st_size or same != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size):
            raise RunnerError(f"{rel} changed while it was read")
        dest = _open_dir("artifacts", agent)
        fds.append(dest)
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=dest)
            except FileExistsError:
                pass
            dest = _open_dir(part, dest)
            fds.append(dest)
        out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dest)
        fds.append(out)
        created = (parts[-1], dest)
        made = os.fstat(out)
        if not stat.S_ISREG(made.st_mode) or made.st_nlink != 1:
            raise RunnerError(f"the destination for {rel} is not a single-link regular file")
        data = b"".join(chunks)
        written = 0
        while written < len(data):
            written += os.write(out, data[written:])
        created = None
        return size
    except (OSError, RunnerError) as error:
        if created:
            os.unlink(created[0], dir_fd=created[1])
        raise error if isinstance(error, RunnerError) else RunnerError(f"promotion of {rel} failed: {error}")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def cmd_promote(args):
    run = Run(args.run)
    if not run.sandbox:
        raise RunnerError("a source-only run executes no target code, so it has nothing to promote")
    calls = run.calls()
    entry = run.call(calls, args.agent_id)
    allow = entry.get("allowlist") or {}
    if args.path not in allow:
        raise RunnerError(f"{args.path} is not on {args.agent_id}'s promotion allowlist")
    done = entry.setdefault("promoted", [])
    if any(p["path"] == args.path for p in done):
        raise RunnerError(f"{args.path} was already promoted")
    total = entry.get("total_bytes") or sum(allow.values())
    size = promote_file(run.root, args.agent_id, args.path, allow[args.path], total - sum(p["bytes"] for p in done))
    artifact = f"agents/{args.agent_id}/artifacts/{args.path}"
    done.append({"path": args.path, "bytes": size, "artifact": artifact})
    run.save(calls=calls)
    print(json.dumps({"artifact": artifact, "bytes": size}))


# ---------------------------------------------------------------- budget, status, finish, reports


def cmd_budget(args):
    run = Run(args.run)
    calls, candidates = run.calls(), run.candidates()
    if args.reserve is not None:
        calls["validation_reserve"] = args.reserve
        run.save(calls=calls)
    print(json.dumps(budget_report(run, calls, candidates), indent=1))


def open_work(run, ledger, candidates, calls):
    gaps = []
    if run.meta.get("incomplete_reason") == "budget_cannot_fund_reconnaissance_and_reserves":
        gaps.append("the budget could not fund reconnaissance and the reserves, so no audit pass ran")
    for status in ("planned", "in_progress"):
        ids = [u["coverage_id"] for u in ledger if u["status"] == status]
        if ids:
            gaps.append(f"{len(ids)} unit(s) still {status}")
    if critics_owed(run, calls) and ledger:
        gaps.append("a required coverage critic has not run")
    unvalidated = sorted(fp for fp, c in candidates.items() if c.get("state") == "unvalidated")
    if unvalidated:
        gaps.append(f"unvalidated candidates: {', '.join(unvalidated)}")
    undecided = sorted({fp for u in ledger if u["status"] == "candidate" for fp in u["result_fingerprints"]
                        if candidates.get(resolve_fp(candidates, fp), {}).get("state") not in VERDICTS + ("unvalidated",)})
    if undecided:
        gaps.append(f"ledger fingerprints with no candidate entry: {', '.join(undecided)}")
    if run.profile != "quick":
        pending = sorted(fp for fp, c in candidates.items() if c.get("state") in ("confirmed", "needs_validation") and c.get("phase5") != "verified")
        if pending:
            gaps.append(f"records without a Phase 5 pass: {', '.join(pending)}")
    return gaps, unvalidated


def cmd_status(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    gaps, _ = open_work(run, ledger, candidates, calls)
    print(json.dumps({
        "run_status": run.meta.get("run_status"), "profile": run.profile, "execution": run.meta.get("execution"),
        "units": {s: sum(u["status"] == s for u in ledger) for s in sorted({u["status"] for u in ledger})},
        "candidates": {s: sum(c.get("state") == s for c in candidates.values()) for s in sorted({c.get("state") for c in candidates.values()})},
        "agents": {c["agent_id"]: f"{c['role']}:{c['status']}" for c in calls["calls"]},
        "budget": budget_report(run, calls, candidates), "open_work": gaps}, indent=1))


def cmd_finish(args):
    run = Run(args.run)
    ledger, calls, candidates = run.ledger(), run.calls(), run.candidates()
    gaps, unvalidated = open_work(run, ledger, candidates, calls)
    unfunded = run.meta.get("incomplete_reason") == "budget_cannot_fund_reconnaissance_and_reserves"
    if gaps and not args.incomplete and not unfunded:
        raise Refused("the run is not complete: " + "; ".join(gaps) + ". Finish the work, or pass --incomplete REASON.")
    for fp, c in candidates.items():
        if c.get("pending_replacement"):
            c["removed"] = "disputed: a material Phase 5 replacement was never independently verified"
    spent = {r: sum(c["role"] == r for c in calls["calls"]) for r in ROLES}
    spent["total"] = len(calls["calls"])
    run.meta.update(agents_spent=spent, run_status="incomplete" if gaps else "complete")
    if gaps:
        reason = run.meta.get("incomplete_reason") if run.meta.get("run_status") == "incomplete" else None
        reason = reason or args.incomplete
        run.meta.update(incomplete_reason=reason, incomplete_detail=gaps, unvalidated_candidates=unvalidated)
        for fp in unvalidated:
            for unit in ledger:
                note_text = f"{reason}: candidate {fp} was not independently validated"
                if unit["coverage_id"] in linked_units(candidates[fp]) and unit["status"] == "candidate" and note_text not in unit["unresolved"]:
                    unit["unresolved"].append(note_text)
    else:
        for key in ("incomplete_reason", "incomplete_detail", "unvalidated_candidates"):
            run.meta.pop(key, None)
    run.save(ledger=ledger, candidates=candidates, findings=True)
    run.save_meta()
    print(json.dumps({"run_status": run.meta["run_status"], "agents_spent": spent, "gaps": gaps}, indent=1))


def bullet_lines(items):
    return "\n".join(f"- {item}" for item in items)


def cmd_needs_validation(args):
    run = Run(args.run)
    ledger, candidates = run.ledger(), run.candidates()
    records = [r for r in read_json(run.file("findings.json"), []) if r.get("verdict") == "needs_validation"]
    by_unit = {u["coverage_id"]: u for u in ledger}
    order = [fp.strip() for fp in (args.order or "").split(",") if fp.strip()]

    def units_of(fp):
        return [by_unit[i] for i in linked_units(candidates.get(fp, {})) if i in by_unit]

    def rank(record):
        fp = record["fingerprint"]
        if fp in order:
            return (0, order.index(fp), fp)
        priorities = [u["assignment_priority"] for u in units_of(fp) if isinstance(u.get("assignment_priority"), (int, float))]
        return (1, min(priorities) if priorities else math.inf, fp)

    records.sort(key=rank)
    partial = []
    if run.profile == "quick":
        partial.append("quick profile")
    if run.meta.get("scope_paths"):
        partial.append("scoped")
    if run.meta.get("execution") == "source-only":
        partial.append("source-only")
    if run.meta.get("run_status") == "incomplete":
        partial.append(f"incomplete: {run.meta.get('incomplete_reason')}")
    lines = ["# Needs validation", "",
             f"Leads from run `{run.meta['run_id']}` ({', '.join(partial) or run.profile}), in priority order. None has a "
             "severity: each is a source-grounded boundary hypothesis blocked on a fact that source and bounded local "
             "checks cannot settle. The resolution plans never send audit traffic to a deployment.", ""]
    if not records:
        lines += ["No needs_validation records in findings.json.", ""]
    for n, record in enumerate(records, 1):
        units = units_of(record["fingerprint"])
        lines += [f"## {n}. {record['title']}", "", f"Fingerprint: `{record['fingerprint']}`", "", record["description"], "",
                  f"**Claimed root cause.** {record['claimed_root_cause']}", ""]
        if units:
            lines += ["**Affected boundary**", "", bullet_lines(sorted({f"{u['boundary']} (surface: {u['surface']})" for u in units})), ""]
        lines += ["**Trace**", ""] + [f"{i}. `{e['kind']}` `{e['file']}:{e['line']}` ({e['scope']}): {e['description']}"
                                     for i, e in enumerate(record["trace"], 1)] + [""]
        lines += ["**Evidence**", "", bullet_lines(f"`{e['file']}:{e['line']}`: {e['description']}" for e in record["evidence"]), ""]
        lines += ["**Blockers**", "", bullet_lines(record["blockers"]), "", "**Resolution plan**", ""]
        plan = record["validation_plan"]
        if plan.get("local"):
            lines.append(f"- Local, bounded: {plan['local']}")
        if plan.get("deployment"):
            lines.append(f"- Owner-observed: {plan['deployment']}")
        lines.append("")
    path = run.file("NEEDS-VALIDATION.md")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(json.dumps({"written": str(path), "records": len(records)}))


def cmd_validate(args):
    run = Run(args.run)
    codes = {}
    for name, script in (("findings.json", "validate-findings.cjs"), ("coverage-ledger.json", "validate-coverage-ledger.cjs")):
        result = run_validator(run.skill, script, run.file(name))
        if result is None:
            raise RunnerError(f"{script} cannot run: node or the validator is missing")
        codes[script] = result[0]
        if result[0]:
            print(f"{script}:\n{result[1]}", file=sys.stderr)
    print(" ".join(f"{k}={v}" for k, v in codes.items()))
    return 1 if any(codes.values()) else 0


# ---------------------------------------------------------------- command line


def parser():
    top = argparse.ArgumentParser(prog="audit_runner.py", description=__doc__.split("\n\n")[0])
    sub = top.add_subparsers(dest="command", required=True)

    def command(name, func, help_text, run=True):
        p = sub.add_parser(name, help=help_text, description=help_text)
        if run:
            p.add_argument("run", help="the run's output directory")
        p.set_defaults(func=func)
        return p

    p = command("init", cmd_init, "Create the run directory and run-metadata.json.", run=False)
    p.add_argument("run", nargs="?", help="output directory (default ~/security-audit-skill/<repo>/run-<N>)")
    p.add_argument("--target", required=True, help="absolute path of the repository under review")
    p.add_argument("--profile", choices=PROFILES, default="standard")
    p.add_argument("--repo", help="stable repository identifier (default: the origin remote, else the folder name)")
    p.add_argument("--budget", type=int, help="maximum agent calls across all phases")
    p.add_argument("--scope", action="append", help="an in-scope repository-relative path (repeat); omit for a full run")
    p.add_argument("--skill-dir", default=DEFAULT_SKILL_DIR, help=f"installed security-audit skill (default {DEFAULT_SKILL_DIR})")
    p.add_argument("--prior", action="append", help="a prior run directory for the same repository (repeat)")
    p.add_argument("--sandbox", help="describe the OS-enforced sandbox that meets every control in the skill's SKILL.md; omit for source-only")
    p.add_argument("--inside-target", action="store_true", help="allow an output directory inside the target that git ignores")
    p.add_argument("--record-unfunded", action="store_true", help="record an unfundable budget as an incomplete run")
    p.add_argument("--without-validators", action="store_true", help="tests only: save without the skill's Node validators")

    p = command("seed", cmd_seed, "Add ledger units from a JSON spec (see SKILL.md).")
    p.add_argument("spec")

    p = command("carry", cmd_carry, "Carry a prior confirmed or needs_validation record into the candidate set.")
    p.add_argument("--prior-findings", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--unit", required=True, help="the planned coverage_id the record links to")

    p = command("assign", cmd_assign, "Set planned units in_progress for one fresh hunter.")
    p.add_argument("agent_id")
    p.add_argument("units", nargs="+", metavar="coverage_id")
    p.add_argument("--over-budget", metavar="REASON", help="record that the user approved going over the budget")

    p = command("unassign", cmd_unassign, "Return a hunter's units to planned when its prompt was never built.")
    p.add_argument("agent_id")

    p = command("defer", cmd_defer, "Mark planned units deferred with a reason, such as a budget stop.")
    p.add_argument("--reason", required=True)
    p.add_argument("--all-planned", action="store_true")
    p.add_argument("units", nargs="*", metavar="coverage_id")

    p = command("prompt", cmd_prompt, "Build agents/<id>/prompt.md and count the call against the budget.", run=False)
    p.add_argument("role", choices=ROLES)
    p.add_argument("run")
    p.add_argument("agent_id")
    p.add_argument("--fingerprint", help="verifier and record-verifier: the candidate")
    p.add_argument("--agent", dest="recon_agent", choices=RECON_AGENTS, help="recon: which baseline agent")
    p.add_argument("--text-file", help="recon: the parent's own prompt for a focused agent")
    p.add_argument("--final-clean", action="store_true", help="critic: the final-clean pass (standard and deep)")
    p.add_argument("--allow-artifact", action="append", help="sandboxed runs: SCRATCH/PATH=BYTES to promote (repeat)")
    p.add_argument("--total-bytes", type=int, help="sandboxed runs: cumulative promotion limit")
    p.add_argument("--over-budget", metavar="REASON", help="record that the user approved going over the budget")

    p = command("extract", cmd_extract, "Save an agent's final reply from its transcript, checked against its contract.")
    p.add_argument("agent_id")
    p.add_argument("transcript", help="the agent's JSONL output file (tasks/<agentId>.output)")

    p = command("apply-hunter", cmd_apply_hunter, "Apply one hunter's reply to the ledger and candidates.json.")
    p.add_argument("agent_id")

    p = command("merge", cmd_merge, "Merge candidates that share one root cause under one fingerprint.")
    p.add_argument("keep")
    p.add_argument("drop", nargs="+")
    p.add_argument("--reason", required=True)
    p.add_argument("--lead", help="the fingerprint whose proposal has the strongest trace (default: keep)")

    p = command("apply-critic", cmd_apply_critic, "Record a critic's missing units and reassignments in the ledger.")
    p.add_argument("agent_id")
    p.add_argument("--refs", help="JSON object: missing-unit number (from 1) to its canonical_refs")
    p.add_argument("--out-of-scope", action="append", metavar="N=REASON")
    p.add_argument("--reject", action="append", metavar="N=REASON")
    p.add_argument("--keep-closed", action="append", metavar="COVERAGE_ID=REASON")
    p.add_argument("--reassign-reason", action="append", metavar="COVERAGE_ID=REASON")

    p = command("apply-verifier", cmd_apply_verifier, "Apply a Phase 3 verifier's decision and rewrite findings.json.")
    p.add_argument("agent_id")
    p.add_argument("--different-root-cause", action="store_true",
                   help="the reply's new fingerprint names a different root cause: add it as a new candidate")

    p = command("apply-record-verifier", cmd_apply_record_verifier, "Apply a Phase 5 record verifier's reply.")
    p.add_argument("agent_id")

    p = command("promote", cmd_promote, "Promote one allowlisted scratch file to the agent's artifacts (sandboxed runs).")
    p.add_argument("agent_id")
    p.add_argument("path", help="scratch-relative path on the agent's allowlist")

    p = command("budget", cmd_budget, "Show agent calls spent, reserves, and how many hunters still fit.")
    p.add_argument("--reserve", type=int, help="set the validation reserve in agent calls")

    command("status", cmd_status, "Summarize units, candidates, agents and open work.")

    p = command("finish", cmd_finish, "Set run_status complete, or incomplete with a reason.")
    p.add_argument("--incomplete", metavar="REASON")

    p = command("needs-validation", cmd_needs_validation, "Write NEEDS-VALIDATION.md from findings.json.")
    p.add_argument("--order", help="comma-separated fingerprints to list first")

    command("validate", cmd_validate, "Run the skill's two Node validators on findings.json and the ledger.")
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except RunnerError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
