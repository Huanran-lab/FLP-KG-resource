"""Write merged graph into Neo4j with MERGE semantics."""
from __future__ import annotations

import json
import re
from typing import Any

from neo4j import GraphDatabase

from .config_utils import load_config, load_schema, neo4j_password, project_root


def _constraint_name(label: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", label).lower()
    return f"{safe}_name_key"


class Neo4jWriter:
    def __init__(
        self,
        *,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> None:
        cfg = load_config()
        schema = load_schema()
        neo = cfg["neo4j"]
        uri = uri or neo.get("uri") or schema.get("neo4j", {}).get("uri", "bolt://localhost:7687")
        user = user or neo.get("user") or schema.get("neo4j", {}).get("user", "neo4j")
        password = password if password is not None else neo4j_password(cfg)
        self.database = database or neo.get("database", "neo4j")
        self.labels = list(schema["node_labels"])
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def verify(self) -> None:
        self.driver.verify_connectivity()

    def ensure_constraints(self) -> None:
        with self.driver.session(database=self.database) as session:
            for label in self.labels:
                session.run(
                    f"CREATE CONSTRAINT {_constraint_name(label)} IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.name_key IS UNIQUE"
                )

    def clear_graph(self) -> None:
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (n) DETACH DELETE n")

    def write_graph(self, graph: dict[str, Any], clear_first: bool = False) -> dict[str, int]:
        if clear_first:
            self.clear_graph()
        self.ensure_constraints()

        nodes = graph["nodes"]
        edges = graph["edges"]

        with self.driver.session(database=self.database) as session:
            for node in nodes:
                label = node["label"]
                if label not in self.labels:
                    continue
                props: dict[str, Any] = {
                    "name": node["name"],
                    "name_key": node["name_key"],
                    "needs_review": bool(node.get("needs_review")),
                }
                for k, v in (node.get("props") or {}).items():
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        props[k] = v
                    else:
                        props[k] = json.dumps(v, ensure_ascii=False)

                # Provenance as node properties (no Document / REPORTED_IN)
                sources = node.get("sources") or []
                if sources:
                    pdfs = sorted(
                        {
                            s.get("source_pdf")
                            for s in sources
                            if s.get("source_pdf")
                        }
                    )
                    props["source_pdfs"] = json.dumps(pdfs, ensure_ascii=False)
                    props["source_pdf"] = sources[0].get("source_pdf")
                    props["page_start"] = sources[0].get("page_start")
                    props["page_end"] = sources[0].get("page_end")

                session.run(
                    f"MERGE (n:{label} {{name_key: $name_key}}) SET n += $props",
                    name_key=node["name_key"],
                    props=props,
                )

            for edge in edges:
                etype = edge["type"]
                s = edge["source"]
                t = edge["target"]
                if s["label"] not in self.labels or t["label"] not in self.labels:
                    continue
                eprops: dict[str, Any] = {}
                for k, v in (edge.get("props") or {}).items():
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        eprops[k] = v
                    else:
                        eprops[k] = json.dumps(v, ensure_ascii=False)
                sources = edge.get("sources") or []
                if sources:
                    eprops["source_pdf"] = sources[0].get("source_pdf")
                    eprops["page_start"] = sources[0].get("page_start")
                    eprops["page_end"] = sources[0].get("page_end")

                session.run(
                    f"MATCH (a:{s['label']} {{name_key: $sk}}) "
                    f"MATCH (b:{t['label']} {{name_key: $tk}}) "
                    f"MERGE (a)-[r:{etype}]->(b) "
                    f"SET r += $props",
                    sk=s["name_key"],
                    tk=t["name_key"],
                    props=eprops,
                )

            stats = session.run(
                "MATCH (n) WITH count(n) AS nodes "
                "MATCH ()-[r]->() RETURN nodes, count(r) AS rels"
            ).single()
            return {"nodes": int(stats["nodes"]), "relationships": int(stats["rels"])}


def write_merged_graph(graph: dict[str, Any], clear_first: bool = True) -> dict[str, int]:
    writer = Neo4jWriter()
    try:
        writer.verify()
        stats = writer.write_graph(graph, clear_first=clear_first)
    finally:
        writer.close()
    out = project_root() / "out" / "merged_graph.json"
    out.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def smoke_queries() -> list[dict[str, Any]]:
    writer = Neo4jWriter()
    results: list[dict[str, Any]] = []
    try:
        writer.verify()
        with writer.driver.session(database=writer.database) as session:
            q1 = session.run(
                "MATCH (m:Material)-[:HAS_DEFECT]->(d:Defect)-[:ENABLES]->(f:FLP)"
                "-[:HAS_ACID_SITE]->(a:LewisAcid) "
                "RETURN m.name AS material, d.name AS defect, f.name AS flp, a.name AS acid LIMIT 20"
            )
            results.append({"query": "canonical_defect_flp_acid", "rows": [r.data() for r in q1]})

            q2 = session.run(
                "MATCH (m:Material) "
                "OPTIONAL MATCH (m)-[:HAS_DEFECT]->(d) "
                "OPTIONAL MATCH (f:FLP)-[:LOCATED_ON]->(m) "
                "OPTIONAL MATCH (f)-[:CATALYZES]->(rx:Reaction) "
                "RETURN m.name AS material, collect(DISTINCT d.name) AS defects, "
                "collect(DISTINCT f.name) AS flps, collect(DISTINCT rx.name) AS reactions "
                "ORDER BY material LIMIT 30"
            )
            results.append({"query": "materials_overview", "rows": [r.data() for r in q2]})

            q3 = session.run(
                "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC"
            )
            results.append({"query": "label_counts", "rows": [r.data() for r in q3]})
    finally:
        writer.close()
    return results
