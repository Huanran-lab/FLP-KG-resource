"""GraphRAG chat: natural language <-> Cypher <-> Neo4j <-> natural language."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from neo4j import GraphDatabase

from .config_utils import load_config, load_schema, neo4j_password, project_root
from .llm_client import HttpLLM, make_llm_client

FORBIDDEN_CYPHER = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL\s+\{|"
    r"apoc\.|dbms\.|db\.|WRITE|FOREACH)\b",
    re.IGNORECASE,
)

# Chinese / informal terms -> English graph entity hints
CN_GLOSSARY: dict[str, list[str]] = {
    "氧空位": ["oxygen vacancy", "Ov", "VO"],
    "空位": ["oxygen vacancy", "vacancy"],
    "氧化铈": ["CeO2", "ceria"],
    "二氧化铈": ["CeO2", "ceria"],
    "ceria": ["CeO2"],
    "氧化铟": ["In2O3"],
    "氢氧化铟": ["In2O3", "indium hydroxide"],
    "氧化锌": ["ZnO"],
    "氧化铝": ["Al2O3"],
    "氧化钛": ["TiO2"],
    "氧化镁": ["MgO"],
    "表面flp": ["surface FLP"],
    "表面FLP": ["surface FLP"],
    "受阻路易斯对": ["surface FLP", "frustrated Lewis pair"],
    "受阻路易斯酸碱对": ["surface FLP"],
    "路易斯酸": ["Lewis acid"],
    "路易斯碱": ["Lewis base"],
    "氢化": ["hydrogenation"],
    "加氢": ["hydrogenation"],
    "二氧化碳": ["CO2"],
    "甲烷": ["CH4", "methane"],
    "氨分解": ["ammonia decomposition"],
    "沸石": ["zeolite"],
    "金属有机框架": ["MOF"],
    "共价有机框架": ["COF"],
    "d带": ["d-band center", "d-band"],
    "p带": ["p-band center", "p-band"],
    "d带中心": ["d-band center"],
    "p带中心": ["p-band center"],
    "轨道杂化": ["d-p hybridization", "orbital hybridization"],
    "活性": ["CO2RR activity", "catalytic activity", "activity"],
    "CO2RR活性": ["CO2RR activity"],
    "二氧化碳还原": ["CO2RR", "CO2RR activity"],
    "选择性和活性": ["CO2RR activity", "CO selectivity"],
    "活化": ["CO2 activation", "activation"],
    "CO2活化": ["CO2 activation"],
    "二氧化碳活化": ["CO2 activation"],
    "促进": ["CO2 activation", "CO2RR activity"],
}


@dataclass
class ChatTurn:
    question: str
    cypher: str
    rows: list[dict[str, Any]]
    answer: str
    hints: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


@dataclass
class GraphRAGChat:
    history: list[ChatTurn] = field(default_factory=list)
    max_rows: int = 40
    max_history: int = 6
    uri: str | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None

    def __post_init__(self) -> None:
        self.cfg = load_config()
        self.schema = load_schema()
        neo = self.cfg["neo4j"]
        uri = (
            self.uri
            or neo.get("uri")
            or self.schema.get("neo4j", {}).get("uri", "bolt://localhost:7687")
        )
        user = (
            self.user
            or neo.get("user")
            or self.schema.get("neo4j", {}).get("user", "neo4j")
        )
        password = (
            self.password if self.password is not None else neo4j_password(self.cfg)
        )
        self.database = (
            self.database
            or neo.get("database")
            or self.schema.get("neo4j", {}).get("database", "neo4j")
        )
        self.uri = uri
        self.user = user
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.llm: HttpLLM = make_llm_client(self.cfg)
        self.model = self.cfg["llm"]["model"]
        self._synonyms = self._load_synonyms()

    def close(self) -> None:
        self.driver.close()
        self.llm.close()

    def _load_synonyms(self) -> dict[str, list[str]]:
        syn = dict(self.schema.get("synonyms") or {})
        path = project_root() / "synonyms_extra.yaml"
        if path.exists():
            syn.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        return syn

    def _schema_block(self) -> str:
        labels = self.schema["node_labels"]
        rels = self.schema["relationship_types"]
        patterns = self.schema.get("canonical_patterns", [])
        return (
            f"Node labels: {labels}\n"
            f"Relationship types: {rels}\n"
            f"Canonical patterns:\n"
            + "\n".join(f"- {p}" for p in patterns)
        )

    def _expand_query_tokens(self, question: str) -> list[str]:
        tokens: list[str] = []
        for m in re.finditer(r'"([^"]+)"|“([^”]+)”|「([^」]+)」', question):
            tokens.append(next(g for g in m.groups() if g))

        # glossary hits (longer keys first)
        q_lower = question.lower()
        for zh, en_list in sorted(CN_GLOSSARY.items(), key=lambda kv: -len(kv[0])):
            if zh.lower() in q_lower or zh in question:
                tokens.extend(en_list)

        tokens.extend(
            re.findall(r"[A-Za-z][A-Za-z0-9α-ωΑ-ΩδΔγΓλΛμΜθΘ/\-−–—()]{1,40}", question)
        )
        tokens.extend(re.findall(r"[\u4e00-\u9fff]{2,12}", question))

        # synonym canonical names if mentioned
        for canon, variants in self._synonyms.items():
            opts = [canon, *(variants or [])]
            for v in opts:
                if v and v.lower() in q_lower:
                    tokens.append(canon)
                    tokens.extend(opts[:4])
                    break

        cleaned: list[str] = []
        stop = {
            "what", "which", "how", "does", "the", "and", "with", "from", "about",
            "是什么", "哪些", "如何", "怎么", "什么", "有没有", "能否", "请问",
            "告诉我", "介绍", "一下", "这个", "那个", "可以", "相关",
        }
        for t in tokens:
            t = t.strip()
            if len(t) < 2 or t.lower() in stop or t in stop:
                continue
            if t not in cleaned:
                cleaned.append(t)
        return cleaned

    def suggest_entities(self, question: str, limit: int = 24) -> list[dict[str, str]]:
        """Lightweight lexicon hints from the graph for entity grounding."""
        cleaned = self._expand_query_tokens(question)
        if not cleaned:
            return []

        hints: list[dict[str, str]] = []
        with self.driver.session(database=self.database) as session:
            for token in cleaned[:16]:
                rows = session.run(
                    """
                    MATCH (n)
                    WHERE n.name IS NOT NULL AND (
                      toLower(n.name) CONTAINS toLower($q)
                      OR (n.name_key IS NOT NULL AND n.name_key CONTAINS toLower(replace($q,' ','')))
                      OR (
                        n.aliases IS NOT NULL AND n.aliases IS :: LIST<STRING>
                        AND any(a IN n.aliases WHERE toLower(a) CONTAINS toLower($q))
                      )
                    )
                    RETURN labels(n)[0] AS label, n.name AS name
                    ORDER BY size(n.name) ASC
                    LIMIT 5
                    """,
                    q=token,
                )
                for r in rows:
                    item = {"label": r["label"], "name": r["name"]}
                    if item not in hints:
                        hints.append(item)
                    if len(hints) >= limit:
                        return hints
        return hints

    def _chat(self, system: str, user: str, temperature: float | None = None) -> str:
        return self.llm.chat(system, user, temperature=temperature)

    def _strip_code_fence(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:cypher|json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def nl_to_cypher(
        self, question: str, hints: list[dict[str, str]], repair_error: str | None = None
    ) -> str:
        hist = ""
        for turn in self.history[-self.max_history :]:
            hist += f"Q: {turn.question}\nCypher: {turn.cypher}\n\n"

        system = f"""You translate questions about solid/heterogeneous Frustrated Lewis Pairs (FLP)
into READ-ONLY Neo4j Cypher queries.

This graph contains SOLID / HETEROGENEOUS / surface FLP knowledge
(oxides, MOFs, COFs, zeolites, supported catalysts, defect-enabled FLPs).

SCHEMA:
{self._schema_block()}

RULES:
1. Output ONLY one Cypher query. No markdown, no comments, no explanation.
2. READ-ONLY only: MATCH / OPTIONAL MATCH / WITH / RETURN / WHERE / ORDER BY / LIMIT / UNION.
   Never CREATE, MERGE, DELETE, SET, REMOVE, DROP, CALL write procedures.
3. Prefer matching entities with:
   toLower(n.name) CONTAINS toLower('...')
   or exact n.name = '...' when the hint list provides a canonical name.
4. Use relationship types and labels EXACTLY as listed in the schema.
5. Always include LIMIT (default {self.max_rows}).
6. Return human-readable fields (n.name, type(r), labels) rather than internal ids.
7. If the question is about mechanisms/paths, return paths with variable-length *1..4 when useful.
8. Node property for identity is usually `name` (and optional `aliases`, `name_key`).
9. Prefer these solid-FLP patterns when relevant:
   surface FLP -CONTAINS-> Material -CONTAINS-> specific LA-LB FLP
   specific FLP -LOCATED_ON-> Material
   Material -HAS_ACID_SITE-> LewisAcid ; Material -HAS_BASE_SITE-> LewisBase
   Material -HAS_DEFECT-> Defect ; Defect/Ov -ENABLES-> specific FLP (or Lewis basicity -FACILITATES-> FLP)
   Material -CATALYZES-> Reaction ; Material -ACTIVATES-> Molecule/Reactant
   Descriptors usually hang on the specific LA-LB FLP (not umbrella surface FLP)
10. CRITICAL relationship directions (do NOT invent reverse directions):
   - CATALYZES is ONLY Material (or similar catalyst host) -> Reaction.
     NEVER write (flp:FLP)-[:CATALYZES]->(r:Reaction).
   - Activity links are usually:
     (FLP|Descriptor|ElectronicDescriptor|GeometricDescriptor)-[:CORRELATES_WITH]->(Activity)
     OR (Descriptor)-[:FACILITATES]->(FLP)-[:CORRELATES_WITH]->(Activity)
   - For CO2RR questions, Reaction.name is often exactly 'CO2RR' and Activity.name 'CO2RR activity'.
   - Mechanism name for CO2 activation is usually exactly 'CO2 activation'.
11. Natural-language intent mapping (user need NOT say schema edge names):
   - 促进 / 有助于 / 导致 / 提高 / 增强 / enables / promotes → use ENABLES, FACILITATES, ACTIVATES, and/or CORRELATES_WITH
   - 活化 CO2 / CO2活化 → Mechanism 'CO2 activation' and/or FLP-[:ACTIVATES]->CO2
   - Typical path: FLP -[:ENABLES|FACILITATES]-> (CO2 activation) -[:CORRELATES_WITH|FACILITATES]-> (CO2RR activity|CO2RR)
     also direct FLP -[:CORRELATES_WITH]-> (CO2RR activity)
12. Chinese questions: map 氧化铈/ceria→CeO2, 氧空位→oxygen vacancy, 表面FLP→surface FLP,
    CO2RR活性/二氧化碳还原活性→CO2RR activity, CO2活化→CO2 activation.
13. When listing FLPs related to CO2RR / 二氧化碳还原 / CO2还原, cover ALL of these paths
    (use UNION so nothing is missed). Do NOT require a single edge type:
    (a) FLP -[:CORRELATES_WITH]-> Activity 'CO2RR activity' (or name CONTAINS 'co2rr')
    (b) FLP -[:ENABLES|FACILITATES]-> Mechanism 'CO2 activation'
    (c) FLP -[:ACTIVATES]-> Molecule/Reactant 'CO2'
    (d) Material -[:CATALYZES]-> Reaction 'CO2RR' AND Material -[:CONTAINS]-> FLP
       (or surface FLP -[:CONTAINS]-> Material -[:CONTAINS]-> FLP)
    Never filter only on ReactionStep 'CO2 reduction' — that misses most records.

FEW-SHOT examples (follow these patterns; questions are natural language):
Q: 与CO2还原相关的FLP有哪些？
MATCH (f:FLP)-[:CORRELATES_WITH]->(a:Activity)
WHERE a.name = 'CO2RR activity' OR toLower(a.name) CONTAINS 'co2rr'
RETURN DISTINCT f.name AS flp, 'activity' AS via, a.name AS detail
UNION
MATCH (f:FLP)-[:ENABLES|FACILITATES]->(m:Mechanism {{name:'CO2 activation'}})
RETURN DISTINCT f.name AS flp, 'activation' AS via, m.name AS detail
UNION
MATCH (f:FLP)-[:ACTIVATES]->(c)
WHERE c.name = 'CO2' AND (c:Molecule OR c:Reactant)
RETURN DISTINCT f.name AS flp, 'activates_CO2' AS via, c.name AS detail
UNION
MATCH (m:Material)-[:CATALYZES]->(r:Reaction {{name:'CO2RR'}})
MATCH (m)-[:CONTAINS]->(f:FLP)
RETURN DISTINCT f.name AS flp, 'material_catalyzes' AS via, m.name AS detail
LIMIT {self.max_rows}

Q: FLP是不是促进了CO2活化和CO2RR活性？
MATCH (f:FLP)-[r1:ENABLES|FACILITATES|ACTIVATES|CORRELATES_WITH]->(m)
WHERE m.name IN ['CO2 activation','CO2'] OR (m:Mechanism AND toLower(m.name) CONTAINS 'co2 activ')
OPTIONAL MATCH (m)-[r2:CORRELATES_WITH|FACILITATES]->(a)
WHERE a.name IN ['CO2RR activity','CO2RR'] OR (a:Activity AND toLower(a.name) CONTAINS 'co2rr')
OPTIONAL MATCH (f)-[r3:CORRELATES_WITH]->(a2:Activity {{name:'CO2RR activity'}})
RETURN f.name AS flp, type(r1) AS to_activation_or_co2, m.name AS mid,
       type(r2) AS mid_to_activity, a.name AS via_activity,
       type(r3) AS direct_rel, a2.name AS direct_activity
LIMIT {self.max_rows}

Q: CO2RR活性跟哪些东西有关？
MATCH (x)-[:CORRELATES_WITH]->(a:Activity {{name:'CO2RR activity'}})
RETURN labels(x)[0] AS src_label, x.name AS source, a.name AS activity
LIMIT {self.max_rows}

Q: 哪些材料催化 CO2RR？
MATCH (m:Material)-[:CATALYZES]->(r:Reaction {{name:'CO2RR'}})
OPTIONAL MATCH (m)-[:CONTAINS]->(f:FLP)
RETURN m.name AS material, r.name AS reaction, collect(DISTINCT f.name) AS flps
LIMIT {self.max_rows}
"""
        user = f"""Conversation so far:
{hist if hist else '(none)'}

Entity hints from the database (prefer these exact names when relevant):
{json.dumps(hints, ensure_ascii=False, indent=2) if hints else '[]'}

Question:
{question}
"""
        if repair_error:
            user += (
                f"\nPrevious Cypher failed or returned no useful rows:\n{repair_error}\n"
                "Rewrite with CORRECT relationship directions. "
                "Remember: FLP does NOT CATALYZES Reaction; use Material-[:CATALYZES]->Reaction "
                "and FLP/Descriptor-[:CORRELATES_WITH]->Activity.\n"
            )

        raw = self._chat(system, user, temperature=0.0)
        cypher = self._strip_code_fence(raw)
        if cypher.startswith("{"):
            try:
                data = json.loads(cypher)
                cypher = data.get("cypher") or data.get("query") or cypher
            except json.JSONDecodeError:
                pass
        return cypher.strip().rstrip(";")

    def assert_readonly(self, cypher: str) -> None:
        if FORBIDDEN_CYPHER.search(cypher):
            raise ValueError(f"Refusing non-read-only Cypher: {cypher}")

    def run_cypher(self, cypher: str) -> list[dict[str, Any]]:
        self.assert_readonly(cypher)
        with self.driver.session(database=self.database) as session:
            result = session.run(cypher)
            rows: list[dict[str, Any]] = []
            for i, record in enumerate(result):
                if i >= self.max_rows:
                    break
                row: dict[str, Any] = {}
                for key in record.keys():
                    val = record[key]
                    row[key] = _neo4j_to_jsonable(val)
                rows.append(row)
            return rows

    def rows_to_answer(
        self,
        question: str,
        cypher: str,
        rows: list[dict[str, Any]],
        error: str | None = None,
    ) -> str:
        system = """You are an expert in solid / heterogeneous Frustrated Lewis Pair (FLP) catalysis.
Answer the user's question in clear natural language (Chinese if the question is Chinese, else English).
Use ONLY the provided Neo4j query results as factual evidence from the knowledge graph.
If results are empty, say what was not found and suggest a narrower question.
Do not invent materials, defects, or reactions absent from the results.
Speak naturally: translate graph edges into ordinary words
(ENABLES/FACILITATES/ACTIVATES ≈ 促进/使能/活化; CORRELATES_WITH ≈ 与…相关/关联).
Do NOT tell the user to rephrase with Cypher relationship names.
When listing FLPs, count DISTINCT flp/source names from the rows and list them all
(or summarize by groups if >15). Never say “仅一条/only one” unless distinct count is truly 1.
Keep the answer concise and technical."""
        payload = {
            "question": question,
            "cypher": cypher,
            "rows": rows[: self.max_rows],
            "error": error,
            "row_count": len(rows),
            "distinct_flp_names": sorted(
                {
                    str(r.get("flp") or r.get("source") or "")
                    for r in rows
                    if r.get("flp") or (r.get("src_label") == "FLP" and r.get("source"))
                }
                - {""}
            ),
        }
        user = json.dumps(payload, ensure_ascii=False, indent=2)
        return self._chat(system, user, temperature=0.2)

    @staticmethod
    def _looks_like_co2rr_flp_list(question: str) -> bool:
        q = question.lower()
        has_co2 = any(
            k in question or k in q
            for k in ("CO2RR", "co2rr", "二氧化碳还原", "CO2还原", "co₂还原", "CO₂还原", "二氧化碳")
        )
        has_flp = "flp" in q or "FLP" in question or "受阻路易斯" in question
        listish = any(
            k in question
            for k in ("哪些", "有哪些", "相关", "列出", "多少", "什么FLP", "哪些FLP")
        )
        return has_co2 and (has_flp or listish)

    def _broad_co2rr_flp_rows(self) -> list[dict[str, Any]]:
        cypher = f"""
MATCH (f:FLP)-[:CORRELATES_WITH]->(a:Activity)
WHERE a.name = 'CO2RR activity' OR toLower(a.name) CONTAINS 'co2rr'
RETURN DISTINCT f.name AS flp, 'activity' AS via, a.name AS detail
UNION
MATCH (f:FLP)-[:ENABLES|FACILITATES]->(m:Mechanism {{name:'CO2 activation'}})
RETURN DISTINCT f.name AS flp, 'activation' AS via, m.name AS detail
UNION
MATCH (f:FLP)-[:ACTIVATES]->(c)
WHERE c.name = 'CO2' AND (c:Molecule OR c:Reactant)
RETURN DISTINCT f.name AS flp, 'activates_CO2' AS via, c.name AS detail
UNION
MATCH (mat:Material)-[:CATALYZES]->(:Reaction {{name:'CO2RR'}})
MATCH (mat)-[:CONTAINS]->(f:FLP)
RETURN DISTINCT f.name AS flp, 'material_catalyzes' AS via, mat.name AS detail
LIMIT {self.max_rows}
"""
        return self.run_cypher(cypher)

    def ask(self, question: str) -> ChatTurn:
        hints = self.suggest_entities(question)
        cypher = self.nl_to_cypher(question, hints)
        rows: list[dict[str, Any]] = []
        err: str | None = None
        try:
            rows = self.run_cypher(cypher)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            try:
                cypher = self.nl_to_cypher(question, hints, repair_error=err)
                rows = self.run_cypher(cypher)
                err = None
            except Exception as exc2:  # noqa: BLE001
                err = str(exc2)
                rows = []

        # Empty result often means wrong relationship direction; one repair pass.
        if not rows and err is None:
            try:
                cypher2 = self.nl_to_cypher(
                    question,
                    hints,
                    repair_error=(
                        "Query returned 0 rows. Likely wrong pattern "
                        "(e.g. FLP-[:CATALYZES]->Reaction). "
                        "Use Material-[:CATALYZES]->Reaction and "
                        "FLP/Descriptor-[:CORRELATES_WITH]->Activity instead."
                    ),
                )
                rows2 = self.run_cypher(cypher2)
                if rows2:
                    cypher = cypher2
                    rows = rows2
            except Exception as exc3:  # noqa: BLE001
                err = str(exc3)

        # Listing CO2RR FLPs: if LLM query is too narrow, use broad UNION fallback.
        if err is None and self._looks_like_co2rr_flp_list(question):
            distinct = {
                str(r.get("flp") or "")
                for r in rows
                if r.get("flp")
            } - {""}
            if len(distinct) < 5:
                try:
                    broad = self._broad_co2rr_flp_rows()
                    broad_flps = {str(r.get("flp") or "") for r in broad} - {""}
                    if len(broad_flps) > len(distinct):
                        cypher = (
                            "/* broad CO2RR-FLP fallback UNION */\n"
                            + """MATCH (f:FLP)-[:CORRELATES_WITH]->(a:Activity)
WHERE a.name = 'CO2RR activity' OR toLower(a.name) CONTAINS 'co2rr'
RETURN DISTINCT f.name AS flp, 'activity' AS via, a.name AS detail
UNION
MATCH (f:FLP)-[:ENABLES|FACILITATES]->(m:Mechanism {name:'CO2 activation'})
RETURN DISTINCT f.name AS flp, 'activation' AS via, m.name AS detail
UNION
MATCH (f:FLP)-[:ACTIVATES]->(c)
WHERE c.name = 'CO2' AND (c:Molecule OR c:Reactant)
RETURN DISTINCT f.name AS flp, 'activates_CO2' AS via, c.name AS detail
UNION
MATCH (mat:Material)-[:CATALYZES]->(:Reaction {name:'CO2RR'})
MATCH (mat)-[:CONTAINS]->(f:FLP)
RETURN DISTINCT f.name AS flp, 'material_catalyzes' AS via, mat.name AS detail
"""
                        )
                        rows = broad
                except Exception as exc4:  # noqa: BLE001
                    if not rows:
                        err = str(exc4)

        answer = self.rows_to_answer(question, cypher, rows, error=err)
        turn = ChatTurn(
            question=question,
            cypher=cypher,
            rows=rows,
            answer=answer,
            hints=hints,
            error=err,
        )
        self.history.append(turn)
        if len(self.history) > 50:
            self.history = self.history[-50:]
        return turn


def _neo4j_to_jsonable(val: Any) -> Any:
    if hasattr(val, "labels") and hasattr(val, "items"):
        props = dict(val)
        return {"_labels": list(val.labels), **props}
    if hasattr(val, "type") and hasattr(val, "items") and hasattr(val, "start_node"):
        return {"_type": val.type, **dict(val)}
    if hasattr(val, "nodes") and hasattr(val, "relationships"):
        return {
            "nodes": [_neo4j_to_jsonable(n) for n in val.nodes],
            "relationships": [_neo4j_to_jsonable(r) for r in val.relationships],
        }
    if isinstance(val, list):
        return [_neo4j_to_jsonable(v) for v in val]
    if isinstance(val, dict):
        return {k: _neo4j_to_jsonable(v) for k, v in val.items()}
    return val
