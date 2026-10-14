from neo4j import GraphDatabase
from dotenv import load_dotenv
import os, re, unicodedata, json
from collections import defaultdict

load_dotenv()
d = GraphDatabase.driver(
    "bolt://localhost:7687", auth=("neo4j", os.getenv("NEO4J_PASSWORD"))
)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\ufb01": "fi",
        "\ufb02": "fl",
    }.items():
        s = s.replace(a, b)
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


with d.session() as session:
    rows = list(
        session.run(
            """
            MATCH (n)
            WHERE n.name IS NOT NULL
            RETURN elementId(n) AS id, labels(n)[0] AS label,
                   n.name AS name, n.name_key AS name_key
            """
        )
    )

# Same label + normalized name
by_label_name = defaultdict(list)
by_name = defaultdict(list)
for r in rows:
    item = {
        "id": r["id"],
        "label": r["label"],
        "name": r["name"],
        "name_key": r["name_key"],
        "n": norm(r["name"]),
    }
    by_label_name[(r["label"], item["n"])].append(item)
    by_name[item["n"]].append(item)

same_label = {k: v for k, v in by_label_name.items() if len(v) > 1}
cross_label = {
    k: v
    for k, v in by_name.items()
    if len({x["label"] for x in v}) > 1
}

# name_key mismatches for same display name
key_mismatch = []
for (label, n), nodes in by_label_name.items():
    keys = {x["name_key"] for x in nodes}
    names = {x["name"] for x in nodes}
    if len(nodes) == 1 and len(names) == 1:
        continue

out = {
    "total": len(rows),
    "same_label_norm_dups": len(same_label),
    "cross_label_same_name": len(cross_label),
    "same_label_examples": [
        {
            "label": k[0],
            "norm": k[1],
            "nodes": [{"name": x["name"], "name_key": x["name_key"], "id": x["id"]} for x in v],
        }
        for k, v in sorted(same_label.items(), key=lambda kv: -len(kv[1]))[:30]
    ],
    "cross_label_examples": [
        {
            "norm": k,
            "labels": sorted({x["label"] for x in v}),
            "nodes": [{"label": x["label"], "name": x["name"], "id": x["id"]} for x in v],
        }
        for k, v in sorted(cross_label.items(), key=lambda kv: -len(kv[1]))[:40]
    ],
}
(open("out/dup_name_audit.json", "w", encoding="utf-8").write(
    json.dumps(out, ensure_ascii=False, indent=2)
))
print(json.dumps({
    "total": out["total"],
    "same_label_norm_dups": out["same_label_norm_dups"],
    "cross_label_same_name": out["cross_label_same_name"],
    "same_label_examples": out["same_label_examples"][:5],
    "cross_label_examples": out["cross_label_examples"][:10],
}, ensure_ascii=False, indent=2))
d.close()
