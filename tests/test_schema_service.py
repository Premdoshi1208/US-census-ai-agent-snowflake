import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.db import run_query


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

SCHEMA_CACHE_FILE = CACHE_DIR / "schema_metadata.json"

_SCHEMA_CACHE: Dict[str, Any] = {
    "data": None,
    "last_loaded": 0,
    "ttl_seconds": 3600,
}

METADATA_SAMPLE_LIMIT = 10000

CODE_KEYS = [
    "COLUMN_NAME",
    "COL_NAME",
    "VARIABLE",
    "VAR_NAME",
    "FIELD",
    "FIELD_NAME",
    "NAME",
    "CODE",
]

LABEL_KEYS = [
    "LABEL",
    "TITLE",
    "DISPLAY_NAME",
    "SHORT_LABEL",
    "VARIABLE_LABEL",
    "DESCRIPTION",
    "DESC",
]

CONCEPT_KEYS = [
    "CONCEPT",
    "SUBJECT",
    "TOPIC",
    "UNIVERSE",
    "CATEGORY",
    "GROUP_LABEL",
]

GROUP_KEYS = [
    "GROUP_CODE",
    "GROUP",
    "TABLE_GROUP",
]


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _string_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).upper(): v for k, v in row.items()}


def _pick_first(row: Dict[str, Any], candidate_keys: List[str]) -> Any:
    for key in candidate_keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _table_parts(table_name: str) -> List[str]:
    return [part for part in str(table_name).upper().split("_") if part]


def _extract_year(table_name: str) -> Optional[int]:
    for part in _table_parts(table_name):
        if len(part) == 4 and part.isdigit():
            return int(part)
    return None


def _extract_group_code(table_name: str) -> str:
    parts = _table_parts(table_name)
    for part in reversed(parts):
        if part.startswith("B") and any(ch.isdigit() for ch in part):
            return part
    return ""


def _is_metadata_table(table_name: str) -> bool:
    upper = str(table_name).upper()
    return (
        "METADATA" in upper
        or upper.startswith("META_")
        or "_META_" in upper
        or upper.endswith("_META")
    )


def _get_all_tables() -> List[str]:
    rows = run_query("SHOW TABLES;")
    names = [str(row["name"]) for row in rows if row.get("name")]
    names.sort()
    return names


def _get_table_columns(table_name: str) -> List[Dict[str, Any]]:
    query = f"DESCRIBE TABLE {_quote_ident(table_name)};"
    rows = run_query(query)

    columns: List[Dict[str, Any]] = []
    for row in rows:
        columns.append(
            {
                "name": row.get("name"),
                "type": row.get("type"),
                "nullable": row.get("null?"),
                "default": row.get("default"),
                "primary_key": row.get("primary key"),
            }
        )
    return columns


def _get_metadata_rows(table_name: str) -> List[Dict[str, Any]]:
    query = f"SELECT * FROM {_quote_ident(table_name)} LIMIT {METADATA_SAMPLE_LIMIT};"
    return run_query(query)


def _build_semantic_catalog(table_names: List[str]) -> Dict[str, Dict[str, str]]:
    catalog: Dict[str, Dict[str, str]] = {}

    for table_name in table_names:
        if not _is_metadata_table(table_name):
            continue

        try:
            rows = _get_metadata_rows(table_name)
        except Exception:
            continue

        for raw_row in rows:
            row = _normalize_row(raw_row)

            code = _string_or_none(_pick_first(row, CODE_KEYS))
            label = _string_or_none(_pick_first(row, LABEL_KEYS))
            concept = _string_or_none(_pick_first(row, CONCEPT_KEYS))
            group_code = _string_or_none(_pick_first(row, GROUP_KEYS))

            if not code:
                continue

            code = code.upper()
            if not label and not concept:
                continue

            entry = catalog.get(code, {"label": "", "concept": "", "group_code": ""})

            if label and not entry["label"]:
                entry["label"] = label
            if concept and not entry["concept"]:
                entry["concept"] = concept
            if group_code and not entry["group_code"]:
                entry["group_code"] = group_code.upper()

            catalog[code] = entry

    return catalog


def _build_schema_metadata() -> List[Dict[str, Any]]:
    all_tables = _get_all_tables()
    semantic_catalog = _build_semantic_catalog(all_tables)

    schema_metadata: List[Dict[str, Any]] = []

    for table_name in all_tables:
        if _is_metadata_table(table_name):
            continue

        try:
            columns = _get_table_columns(table_name)
            table_year = _extract_year(table_name)
            table_group_code = _extract_group_code(table_name)

            enriched_columns: List[Dict[str, Any]] = []
            for col in columns:
                col_name = str(col.get("name") or "").upper()
                semantic = semantic_catalog.get(col_name, {})

                enriched_columns.append(
                    {
                        **col,
                        "label": semantic.get("label", ""),
                        "concept": semantic.get("concept", ""),
                        "group_code": semantic.get("group_code", "") or table_group_code,
                    }
                )

            schema_metadata.append(
                {
                    "table_name": table_name,
                    "year": table_year,
                    "group_code": table_group_code,
                    "columns": enriched_columns,
                }
            )
        except Exception as e:
            schema_metadata.append(
                {
                    "table_name": table_name,
                    "year": _extract_year(table_name),
                    "group_code": _extract_group_code(table_name),
                    "columns": [],
                    "error": str(e),
                }
            )

    schema_metadata.sort(
        key=lambda x: ((x.get("year") or 0), x.get("table_name", "")),
        reverse=True,
    )
    return schema_metadata


def _write_schema_to_disk(schema: List[Dict[str, Any]]) -> None:
    payload = {
        "saved_at": time.time(),
        "schema": schema,
    }
    with open(SCHEMA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _read_schema_from_disk() -> Optional[List[Dict[str, Any]]]:
    if not SCHEMA_CACHE_FILE.exists():
        return None

    try:
        with open(SCHEMA_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        schema = payload.get("schema")
        return schema if isinstance(schema, list) else None
    except Exception:
        return None


def _is_disk_cache_fresh() -> bool:
    if not SCHEMA_CACHE_FILE.exists():
        return False
    modified_time = SCHEMA_CACHE_FILE.stat().st_mtime
    age_seconds = time.time() - modified_time
    return age_seconds < _SCHEMA_CACHE["ttl_seconds"]


def get_schema_metadata(force_refresh: bool = False) -> List[Dict[str, Any]]:
    now = time.time()

    if (
        not force_refresh
        and _SCHEMA_CACHE["data"] is not None
        and now - _SCHEMA_CACHE["last_loaded"] < _SCHEMA_CACHE["ttl_seconds"]
    ):
        return _SCHEMA_CACHE["data"]

    if not force_refresh and _is_disk_cache_fresh():
        disk_schema = _read_schema_from_disk()
        if disk_schema is not None:
            _SCHEMA_CACHE["data"] = disk_schema
            _SCHEMA_CACHE["last_loaded"] = now
            return disk_schema

    schema = _build_schema_metadata()
    _write_schema_to_disk(schema)

    _SCHEMA_CACHE["data"] = schema
    _SCHEMA_CACHE["last_loaded"] = now
    return schema


def get_schema_text_summary(max_tables: int = 10, max_columns: int = 40) -> str:
    schema = get_schema_metadata()

    summary_lines: List[str] = []
    for table in schema[:max_tables]:
        table_name = table["table_name"]
        year = table.get("year")
        columns = table.get("columns", [])

        col_details = []
        for col in columns[:max_columns]:
            name = col.get("name")
            col_type = col.get("type")
            label = col.get("label", "")
            concept = col.get("concept", "")
            if name and col_type:
                semantic = " | ".join([x for x in [label, concept] if x])
                if semantic:
                    col_details.append(f"{name} ({col_type}) -- {semantic}")
                else:
                    col_details.append(f"{name} ({col_type})")

        header = f'Table: "{table_name}"'
        if year:
            header += f" | Year: {year}"

        block = header + "\nColumns:\n" + "\n".join(col_details)
        summary_lines.append(block)

    return "\n\n".join(summary_lines)


def warm_schema_cache() -> None:
    get_schema_metadata(force_refresh=False)


def refresh_schema_cache() -> None:
    get_schema_metadata(force_refresh=True)