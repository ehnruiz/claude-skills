#!/usr/bin/env python3
"""List skill pairs whose descriptions overlap enough to fight over the same request.

Usage: find_collisions.py [--threshold 0.25] [--json] [--reviewed FILE] ROOT [ROOT ...]

Each ROOT is searched recursively for SKILL.md files. The score is the cosine
similarity of the two descriptions' TF-IDF vectors, so words every skill uses
("use", "when", "user") count for little and shared distinctive words count for
a lot. Skills with `disable-model-invocation: true` run only when typed by name,
so they cannot collide and are left out of pairs. Same-name skills in different places are reported separately: only one
of them can load.

--reviewed reads the pairs a person has already judged (reviewed-pairs.json,
written by the audit). A reviewed pair is listed but not flagged while both
descriptions still match its hash; a changed description, a missing skill or an
incomplete set of saved requests is flagged.

Exits 0 when nothing is flagged, 1 when a pair, a duplicate name or a review
problem is flagged, 2 on a usage error.
"""

import argparse
import hashlib
import json
import math
import pathlib
import re
import sys
from collections import Counter

STOPWORDS = set(
    """a an and any are as at be by can do does for from has have how i if in into is it its
    me my of on or our so that the their them then there these this to up use used uses using
    via want wants was we what when whenever where which while who will with you your also
    asks ask user users skill skills claude like such than one more other""".split()
)
WORD = re.compile(r"[a-z][a-z0-9'-]+")


def parse_frontmatter(text):
    """Return name and description from a SKILL.md, folding multi-line YAML values."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields, key = {}, None
    for line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            fields[key] = "" if value in {">", "|", ">-", "|-"} else value.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def load_skills(roots):
    skills = []
    for root in roots:
        for path in sorted(pathlib.Path(root).expanduser().rglob("SKILL.md")):
            fields = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            description = fields.get("description", "")
            if description:
                skills.append(
                    {
                        "name": fields.get("name") or path.parent.name,
                        "path": str(path),
                        "description": description,
                        "manual": fields.get("disable-model-invocation", "").lower() == "true",
                    }
                )
    return skills


def singular(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(text):
    words = [re.sub(r"'s$", "", w).strip("'-") for w in WORD.findall(text.lower().replace("’", "'"))]
    return [singular(w) for w in words if w and w not in STOPWORDS]


def vectors(skills):
    counts = [Counter(tokens(s["description"])) for s in skills]
    document_frequency = Counter(word for count in counts for word in count)
    total = len(skills)
    result = []
    for count in counts:
        vector = {w: n * (math.log((1 + total) / (1 + document_frequency[w])) + 1) for w, n in count.items()}
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1.0
        result.append({w: v / norm for w, v in vector.items()})
    return result


def find_pairs(skills, threshold):
    vecs = vectors(skills)
    pairs = []
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            if skills[i]["name"] == skills[j]["name"] or skills[i].get("manual") or skills[j].get("manual"):
                continue
            shared = set(vecs[i]) & set(vecs[j])
            score = sum(vecs[i][w] * vecs[j][w] for w in shared)
            if score >= threshold:
                top = sorted(shared, key=lambda w: (-(vecs[i][w] * vecs[j][w]), w))[:6]
                pairs.append({"a": skills[i], "b": skills[j], "score": round(score, 3), "shared": top})
    return sorted(pairs, key=lambda p: -p["score"])


def find_duplicate_names(skills):
    by_name = {}
    for skill in skills:
        by_name.setdefault(skill["name"], []).append(skill["path"])
    return {name: paths for name, paths in by_name.items() if len(paths) > 1}


def description_hash(skills, pair):
    """A short hash of both descriptions, so editing either one reopens the pair's review."""
    by_name = {s["name"]: s["description"] for s in skills}
    text = "\n".join(f"{name}\n{by_name[name]}" for name in sorted(pair))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_reviewed(path):
    """Read reviewed-pairs.json into {frozenset(pair): {"hash", "requests": [(request, skill or None)]}}.

    A missing file means nothing has been reviewed yet. A malformed one, or a folder, raises ValueError.
    """
    path = pathlib.Path(path).expanduser()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"{path}: cannot read: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        raise ValueError(f"{path}: expected an object with a \"pairs\" list")
    reviewed = {}
    for entry in data["pairs"]:
        names = entry.get("skills") if isinstance(entry, dict) else None
        if not isinstance(names, list) or len(names) != 2 or not all(isinstance(n, str) for n in names) or names[0] == names[1]:
            raise ValueError(f"{path}: each pair names two different skills: {entry!r}")
        pair = frozenset(names)
        if pair in reviewed:
            raise ValueError(f"{path}: {' <-> '.join(sorted(pair))} is listed twice")
        digest, items = entry.get("hash", ""), entry.get("requests", [])
        if not isinstance(digest, str):
            raise ValueError(f"{path}: {' <-> '.join(sorted(pair))}: hash is a string, empty until set")
        if not isinstance(items, list):
            raise ValueError(f"{path}: {' <-> '.join(sorted(pair))}: requests is a list of {{\"request\", \"expect\"}}")
        requests = []
        for item in items:
            request, expect = (item.get("request"), item.get("expect")) if isinstance(item, dict) else (None, None)
            if not isinstance(request, str) or not request.strip() or not (expect is None or isinstance(expect, str)):
                raise ValueError(f"{path}: a request needs text and an expected skill or null: {item!r}")
            requests.append((request, expect))
        reviewed[pair] = {"hash": digest, "requests": requests}
    return reviewed


def review_problems(skills, reviewed):
    """Reviewed pairs that need judging again: a skill is gone, a description changed, or the saved
    requests do not cover both skills with three requests plus one that neither skill should take."""
    names = {s["name"] for s in skills}
    problems = []
    for pair, review in reviewed.items():
        a, b = sorted(pair)
        label = f"{a} <-> {b}"
        if not pair <= names:
            problems.append(f"{label}: a skill no longer exists; remove the entry")
            continue
        current = description_hash(skills, pair)
        if not review["hash"]:
            problems.append(f"{label}: not hashed yet; set its hash to {current}")
        elif current != review["hash"]:
            problems.append(
                f"{label}: a description changed since the review; judge the pair again "
                f"with the skill's step 3, replay its requests, then set its hash to {current}"
            )
        expected = [skill for _, skill in review["requests"]]
        outside = sorted({skill for skill in expected if skill is not None and skill not in pair})
        if outside:
            problems.append(f"{label}: a request expects {', '.join(outside)}, outside the pair; use null for neither")
        elif sum(skill is not None for skill in expected) < 3:
            problems.append(f"{label}: fewer than three saved requests for the pair")
        else:
            for name in (a, b):
                if name not in expected:
                    problems.append(f"{label}: no request reaches {name}")
        if None not in expected:
            problems.append(f"{label}: no saved request that neither skill should take")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reviewed", help="reviewed-pairs.json listing the pairs already judged")
    args = parser.parse_args(argv)
    missing = [r for r in args.roots if not pathlib.Path(r).expanduser().is_dir()]
    if missing:
        print("not a directory: " + ", ".join(missing), file=sys.stderr)
        return 2

    try:
        reviewed = load_reviewed(args.reviewed) if args.reviewed else {}
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    skills = load_skills(args.roots)
    pairs = find_pairs(skills, args.threshold)
    duplicates = find_duplicate_names(skills)
    problems = review_problems(skills, reviewed)
    for pair in pairs:
        pair["reviewed"] = frozenset({pair["a"]["name"], pair["b"]["name"]}) in reviewed
    open_pairs = [p for p in pairs if not p["reviewed"]]

    if args.json:
        print(json.dumps({"skills": len(skills), "pairs": pairs, "duplicates": duplicates, "review_problems": problems}, indent=2))
    else:
        print(
            f"{len(skills)} skills, {len(open_pairs)} overlapping pairs, {len(pairs) - len(open_pairs)} reviewed, "
            f"{len(duplicates)} duplicate names"
        )
        for pair in pairs:
            note = "  (reviewed)" if pair["reviewed"] else ""
            print(f"\n{pair['score']:.2f}  {pair['a']['name']}  <->  {pair['b']['name']}{note}")
            print(f"      shared: {', '.join(pair['shared'])}")
            print(f"      {pair['a']['path']}\n      {pair['b']['path']}")
        for name, paths in duplicates.items():
            print(f"\nduplicate name: {name}")
            for path in paths:
                print(f"      {path}")
        for problem in problems:
            print(f"\nreview: {problem}")
    return 1 if open_pairs or duplicates or problems else 0


if __name__ == "__main__":
    sys.exit(main())
