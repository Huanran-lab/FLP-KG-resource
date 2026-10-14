# Submission package (KG + GraphRAG QA)

This folder is a **copy** of the files needed to reproduce the solid/heterogeneous FLP knowledge graph and GraphRAG Q&A. Original project files were not modified.

## Contents

| Path | Role |
|------|------|
| `flp_schema.yaml` | KG schema (node/edge types) |
| `synonyms_extra.yaml` | Entity synonym / alias map |
| `config.yaml` | Pipeline + LLM + Neo4j settings (no secrets) |
| `.env.example` | Template for `NEO4J_PASSWORD` |
| `requirements.txt` | Python dependencies |
| `README.md` | Upstream setup notes |
| `src/` | Extraction, merge, Neo4j writer, GraphRAG chat |
| `scripts/` | Pipeline, import, QA launchers, graph cleanup helpers |
| `data/edited/` | **74** curated per-paper KG JSON files (final graph) |
| `data/examples/` | Sample QA outputs / import metadata (optional) |

**Not included (by design):** API keys, `.env`, PDF corpus, Neo4j database dumps, local browser-automation helpers.

## Quick start (import curated KG + QA)

```powershell
cd submit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env: set NEO4J_PASSWORD
# Create api.txt in this folder with your DeepSeek API key (one line), or point config paths as needed

# Start your Neo4j instance, then import curated graphs:
.\.venv\Scripts\python.exe scripts\import_json_to_neo4j.py --dir data\edited

# Terminal QA
.\.venv\Scripts\python.exe scripts\chat_kg.py -q "Your question"

# Web QA
.\.venv\Scripts\python.exe scripts\chat_kg_web.py
```

## Full re-extraction (optional)

Requires a local PDF directory. Set `paths.pdf_dir` in `config.yaml`, then:

```powershell
.\.venv\Scripts\python.exe scripts\run_pipeline.py
```

For the paper artifact, **`data/edited/`** is the authoritative curated knowledge graph.
