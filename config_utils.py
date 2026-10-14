"""Shared config / schema loading helpers."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def project_root() -> Path:
    return ROOT


def load_config(path: Path | None = None) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    cfg_path = path or (ROOT / "config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_schema(path: Path | None = None) -> dict[str, Any]:
    cfg = load_config()
    schema_path = path or (ROOT / cfg["paths"]["schema"])
    with open(schema_path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)

    # Optional extra synonyms (does not modify flp_schema.yaml)
    extra_path = ROOT / cfg["paths"].get("synonyms_extra", "synonyms_extra.yaml")
    if extra_path.exists():
        with open(extra_path, encoding="utf-8") as f:
            extra = yaml.safe_load(f) or {}
        merged = dict(schema.get("synonyms") or {})
        for canon, variants in extra.items():
            existing = list(merged.get(canon, []))
            for v in variants or []:
                if v not in existing:
                    existing.append(v)
            if canon not in existing:
                existing.append(canon)
            merged[canon] = existing
        schema["synonyms"] = merged
    return schema


def read_api_key(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    key_file = ROOT / cfg["paths"]["api_key_file"]
    key = key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"Empty API key in {key_file}")
    return key


def neo4j_password(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    env_name = cfg["neo4j"].get("password_env", "NEO4J_PASSWORD")
    return os.getenv(env_name, "neo4j")


def ensure_out_dir(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    out = ROOT / cfg["paths"]["out_dir"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "chunks").mkdir(exist_ok=True)
    (out / "extractions").mkdir(exist_ok=True)
    return out
