# Solid FLP Knowledge Graph (zongshu MVP)

## Setup

```powershell
cd C:\Users\huanr\Desktop\k
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env: set NEO4J_PASSWORD to your Neo4j password
```

## Start Neo4j

```powershell
.\scripts\start_neo4j.ps1
# Browser: http://localhost:7474
```

First login may require changing the default password; put the new password in `.env`.

## Run pipeline

```powershell
python scripts\run_pipeline.py
```

Useful flags:

- `--skip-pdf` reuse `out/chunks_all.json`
- `--skip-llm` reuse `out/extractions_all.json`
- `--skip-neo4j` only build merged graph + report
- `--no-clear` append without wiping the DB
- `--no-resume` re-extract all chunks

## Chat with the knowledge graph (GraphRAG)

Pipeline:

1. 自然语言问题 → 图谱实体锚定（中英对照 + aliases）
2. LLM 生成**只读** Cypher
3. Neo4j 执行查询
4. LLM 把结果总结成自然语言回答

### 网页对话（推荐）

对接 Desktop 实例 **edit**（`out/edited` 图谱，Bolt `7690`）：

```powershell
cd C:\Users\huanr\Desktop\k
.\scripts\chat_kg_edit.cmd
```

或默认 config / `.env` 里的实例：

```powershell
.\scripts\chat_kg_web.cmd
```

浏览器打开 `http://127.0.0.1:8765/`，可看到锚定实体、生成的 Cypher 与回答。

### 终端对话

```powershell
cd C:\Users\huanr\Desktop\k
.\scripts\chat_kg.cmd --uri bolt://localhost:7690 --password editedit
```

单次提问：

```powershell
.\.venv\Scripts\python.exe scripts\chat_kg.py --uri bolt://localhost:7690 --password editedit -q "CeO2上的氧空位如何形成FLP？"
```

REPL 命令：`/cypher` 切换显示查询，`/rows` 切换原始结果，`/exit` 退出。

## Edit JSON then re-import to Neo4j

1. 编辑抽取 JSON（推荐先复制再改）：
   - 原文件：`out\ref_expand\extractions\*.json`
   - 可复制到：`out\edited\` 再改
2. JSON 结构：`nodes`（label/name/props）+ `edges`（type/source/target/props）
3. 导入（默认合并进现有图，编辑内容优先）：

```powershell
cd C:\Users\huanr\Desktop\k
# 先演练，不写库
.\.venv\Scripts\python.exe scripts\import_json_to_neo4j.py --dir out\edited --dry-run

# 正式写入
.\.venv\Scripts\python.exe scripts\import_json_to_neo4j.py --dir out\edited
```

单文件：

```powershell
.\.venv\Scripts\python.exe scripts\import_json_to_neo4j.py --files out\edited\example_edit_me.json
```

清空后按某目录全量重建（危险）：

```powershell
.\.venv\Scripts\python.exe scripts\import_json_to_neo4j.py --dir out\ref_expand\extractions --clear
```

报告：`out\import_json_report.json`、预览图 `out\import_json_preview_graph.json`。
