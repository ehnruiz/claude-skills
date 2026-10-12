#!/usr/bin/env python3
"""List skill pairs whose descriptions overlap enough to fight over the same request.

Usage: find_collisions.py [--threshold 0.25] [--json] ROOT [ROOT ...]

Each ROOT is searched recursively for SKILL.md files. The score is the cosine
similarity of the two descriptions' TF-IDF vectors, so words every skill uses
("use", "when", "user") count for little and shared distinctive words count for
a lot. Skills with `disable-model-invocation: true` run only when typed by name,
so they cannot collide and are left out of pairs. Same-name skills in different places are reported separately: only one
of them can load.

Exits 0 when nothing is flagged, 1 when a pair or a duplicate name is flagged,
2 on a usage error.
"""

import argparse
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
                top = sorted(shared, key=lambda w: -(vecs[i][w] * vecs[j][w]))[:6]
                pairs.append({"a": skills[i], "b": skills[j], "score": round(score, 3), "shared": top})
    return sorted(pairs, key=lambda p: -p["score"])


def find_duplicate_names(skills):
    by_name = {}
    for skill in skills:
        by_name.setdefault(skill["name"], []).append(skill["path"])
    return {name: paths for name, paths in by_name.items() if len(paths) > 1}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    missing = [r for r in args.roots if not pathlib.Path(r).expanduser().is_dir()]
    if missing:
        print("not a directory: " + ", ".join(missing), file=sys.stderr)
        return 2

    skills = load_skills(args.roots)
    pairs = find_pairs(skills, args.threshold)
    duplicates = find_duplicate_names(skills)

    if args.json:
        print(json.dumps({"skills": len(skills), "pairs": pairs, "duplicates": duplicates}, indent=2))
    else:
        print(f"{len(skills)} skills, {len(pairs)} overlapping pairs, {len(duplicates)} duplicate names")
        for pair in pairs:
            print(f"\n{pair['score']:.2f}  {pair['a']['name']}  <->  {pair['b']['name']}")
            print(f"      shared: {', '.join(pair['shared'])}")
            print(f"      {pair['a']['path']}\n      {pair['b']['path']}")
        for name, paths in duplicates.items():
            print(f"\nduplicate name: {name}")
            for path in paths:
                print(f"      {path}")
    return 1 if pairs or duplicates else 0


if __name__ == "__main__":
    sys.exit(main())
