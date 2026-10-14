"""Export surface FLP ego network for visualization / Neo4j Browser."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config_utils import neo4j_password  # noqa: E402

load_dotenv(ROOT / ".env")
OUT = ROOT / "out" / "surface_flp_neighborhood.json"

d = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", neo4j_password()))
with d.session() as s:
    cores = [
        dict(r)
        for r in s.run(
            """
            MATCH (f:FLP)
            WHERE toLower(f.name) CONTAINS 'surface flp'
               OR coalesce(f.name_key,'') CONTAINS 'surfaceflp'
               OR coalesce(f.name_key,'') = 'surface flp'
            RETURN elementId(f) AS id, f.name AS name,
                   size([(f)--()|1]) AS deg, f.aliases AS aliases
            ORDER BY deg DESC
            """
        )
    ]
    print("cores:")
    for c in cores:
        print(f"  deg={c['deg']}  {c['name']}")

    # Focus on canonical surface FLP
    hops = [
        dict(r)
        for r in s.run(
            """
            MATCH (f:FLP)
            WHERE f.name = 'surface FLP' OR f.name_key = 'surface flp'
            WITH f ORDER BY size([(f)--()|1]) DESC LIMIT 1
            MATCH (f)-[r]-(n)
            WHERE n.name IS NOT NULL
            RETURN type(r) AS rel,
                   CASE WHEN startNode(r) = f THEN 'out' ELSE 'in' END AS dir,
                   labels(n)[0] AS lab,
                   n.name AS name,
                   size([(n)--()|1]) AS deg
            ORDER BY lab, deg DESC, name
            """
        )
    ]

    # Also include other surface-named FLPs and 1-hop materials/defects only (cap)
    related_flps = [
        dict(r)
        for r in s.run(
            """
            MATCH (f:FLP)
            WHERE toLower(f.name) CONTAINS 'surface'
              AND f.name <> 'surface FLP'
            OPTIONAL MATCH (f)-[:LOCATED_ON|SUPPORTED_ON]->(m)
            OPTIONAL MATCH (d:Defect)-[:ENABLES]->(f)
            RETURN f.name AS flp, size([(f)--()|1]) AS deg,
                   collect(DISTINCT m.name)[0..5] AS materials,
                   collect(DISTINCT d.name)[0..5] AS defects
            ORDER BY deg DESC
            LIMIT 40
            """
        )
    ]

    by_lab = Counter(h["lab"] for h in hops)
    by_rel = Counter(h["rel"] for h in hops)

    # Build capped graph for viz: keep top neighbors per label
    keep_per_lab = {
        "Reaction": 25,
        "Material": 20,
        "Catalyst": 15,
        "Defect": 15,
        "LewisAcid": 12,
        "LewisBase": 12,
        "Activity": 12,
        "ElectronicDescriptor": 10,
        "Descriptor": 10,
        "Molecule": 10,
        "Product": 8,
        "Mechanism": 8,
        "ActiveSite": 8,
    }
    selected = []
    counts: dict[str, int] = {}
    for h in hops:
        lab = h["lab"] or "?"
        lim = keep_per_lab.get(lab, 6)
        if counts.get(lab, 0) >= lim:
            continue
        counts[lab] = counts.get(lab, 0) + 1
        selected.append(h)

    payload = {
        "core": cores[0] if cores else None,
        "all_surface_named_flps": cores,
        "one_hop_total": len(hops),
        "one_hop_by_label": dict(by_lab.most_common()),
        "one_hop_by_rel": dict(by_rel.most_common()),
        "viz_edges": selected,
        "other_surface_flps": related_flps,
        "browser_cypher": """
MATCH (f:FLP)
WHERE f.name = 'surface FLP' OR f.name_key = 'surface flp'
WITH f ORDER BY size([(f)--()|1]) DESC LIMIT 1
MATCH path = (f)-[*1..2]-(n)
WHERE n.name IS NOT NULL
RETURN path
LIMIT 200
""".strip(),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n1-hop total: {len(hops)}")
    print("by label:", dict(by_lab.most_common(15)))
    print("by rel:", dict(by_rel.most_common(15)))
    print(f"viz edges kept: {len(selected)}")
    print(f"wrote {OUT}")
d.close()
