# import json
# import re
# import threading
# import time
# from pathlib import Path
# from typing import Any, Dict, List, Optional

# from backend.db import run_query


# BASE_DIR = Path(__file__).resolve().parent
# CACHE_DIR = BASE_DIR / "cache"
# CACHE_DIR.mkdir(exist_ok=True)

# SCHEMA_CACHE_FILE = CACHE_DIR / "schema_metadata.json"
# _CACHE_LOCK = threading.RLock()

# _SCHEMA_CACHE: Dict[str, Any] = {
#     "table_registry": None,
#     "semantic_catalog": None,
#     "table_details": {},
#     "last_loaded": 0,
#     "ttl_seconds": 3600,
# }


# def _quote_ident(name: str) -> str:
#     return '"' + str(name).replace('"', '""') + '"'


# def _table_parts(table_name: str) -> List[str]:
#     return [part for part in str(table_name).upper().split("_") if part]


# def _extract_year(table_name: str) -> Optional[int]:
#     for part in _table_parts(table_name):
#         if len(part) == 4 and part.isdigit():
#             return int(part)
#     return None


# def _extract_group_code(table_name: str) -> str:
#     for part in reversed(_table_parts(table_name)):
#         if re.match(r"^[A-Z]\d+$", part):
#             return part
#     return ""


# def _extract_group_bucket(group_code: str) -> str:
#     digits = "".join(ch for ch in str(group_code) if ch.isdigit())
#     return digits[:2] if digits else ""


# def _is_field_description_table(table_name: str) -> bool:
#     upper = str(table_name).upper()
#     return "FIELD_DESCRIPTIONS" in upper


# def _is_geographic_table(table_name: str) -> bool:
#     upper = str(table_name).upper()
#     return "GEOGRAPHIC_DATA" in upper


# def _is_fips_table(table_name: str) -> bool:
#     upper = str(table_name).upper()
#     return "FIPS_CODES" in upper


# def _is_geometry_table(table_name: str) -> bool:
#     upper = str(table_name).upper()
#     return "GEOMETRY" in upper


# def _table_kind(table_name: str) -> str:
#     if _is_field_description_table(table_name):
#         return "dictionary"
#     if _is_geographic_table(table_name):
#         return "geography"
#     if _is_fips_table(table_name):
#         return "fips"
#     if _is_geometry_table(table_name):
#         return "geometry"
#     if "PATTERNS" in str(table_name).upper():
#         return "patterns"
#     return "fact"


# def _get_all_tables() -> List[str]:
#     rows = run_query("SHOW TABLES;")
#     names = [str(row["name"]) for row in rows if row.get("name")]
#     names.sort()
#     return names


# def _get_table_columns(table_name: str) -> List[Dict[str, Any]]:
#     rows = run_query(f'DESCRIBE TABLE {_quote_ident(table_name)};')
#     cols: List[Dict[str, Any]] = []

#     for row in rows:
#         cols.append(
#             {
#                 "name": row.get("name"),
#                 "type": row.get("type"),
#                 "nullable": row.get("null?"),
#                 "default": row.get("default"),
#                 "primary_key": row.get("primary key"),
#             }
#         )
#     return cols


# def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
#     return {str(k).upper(): v for k, v in row.items()}


# def _build_table_registry() -> List[Dict[str, Any]]:
#     registry: List[Dict[str, Any]] = []

#     for table_name in _get_all_tables():
#         group_code = _extract_group_code(table_name)
#         registry.append(
#             {
#                 "table_name": table_name,
#                 "year": _extract_year(table_name),
#                 "group_code": group_code,
#                 "group_bucket": _extract_group_bucket(group_code),
#                 "table_kind": _table_kind(table_name),
#             }
#         )

#     registry.sort(key=lambda x: ((x.get("year") or 0), x["table_name"]), reverse=True)
#     return registry


# def _build_semantic_catalog(table_registry: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
#     catalog: Dict[str, Dict[str, str]] = {}

#     for item in table_registry:
#         table_name = item["table_name"]
#         if item["table_kind"] != "dictionary":
#             continue

#         try:
#             rows = run_query(f"SELECT * FROM {_quote_ident(table_name)}")
#         except Exception:
#             continue

#         for raw_row in rows:
#             row = _normalize_row(raw_row)
#             column_id = str(row.get("COLUMN_ID") or "").strip().upper()
#             field_name = str(row.get("FIELD_NAME") or "").strip()
#             topic = str(row.get("COLUMN_TOPIC") or "").strip()
#             universe = str(row.get("COLUMN_UNIVERSE") or "").strip()

#             if not column_id:
#                 continue

#             current = catalog.get(
#                 column_id,
#                 {
#                     "label": "",
#                     "topic": "",
#                     "universe": "",
#                 },
#             )

#             if field_name and not current["label"]:
#                 current["label"] = field_name
#             if topic and not current["topic"]:
#                 current["topic"] = topic
#             if universe and not current["universe"]:
#                 current["universe"] = universe

#             catalog[column_id] = current

#     return catalog


# KNOWN_JOIN_KEYS = [
#     "CENSUS_BLOCK_GROUP",
#     "GEOID",
#     "GEOID20",
#     "GEOID10",
#     "BLOCK_GROUP",
#     "CBG",
# ]


# def _detect_join_keys(columns: List[Dict[str, Any]]) -> List[str]:
#     names = {str(col.get("name") or "").upper() for col in columns}
#     return [key for key in KNOWN_JOIN_KEYS if key in names]


# def _write_cache_to_disk() -> None:
#     payload = {
#         "saved_at": time.time(),
#         "table_registry": _SCHEMA_CACHE["table_registry"],
#         "semantic_catalog": _SCHEMA_CACHE["semantic_catalog"],
#         "table_details": _SCHEMA_CACHE["table_details"],
#     }
#     with open(SCHEMA_CACHE_FILE, "w", encoding="utf-8") as f:
#         json.dump(payload, f, indent=2)


# def _read_cache_from_disk() -> None:
#     if not SCHEMA_CACHE_FILE.exists():
#         return

#     try:
#         with open(SCHEMA_CACHE_FILE, "r", encoding="utf-8") as f:
#             payload = json.load(f)

#         if isinstance(payload.get("table_registry"), list):
#             _SCHEMA_CACHE["table_registry"] = payload["table_registry"]
#         if isinstance(payload.get("semantic_catalog"), dict):
#             _SCHEMA_CACHE["semantic_catalog"] = payload["semantic_catalog"]
#         if isinstance(payload.get("table_details"), dict):
#             _SCHEMA_CACHE["table_details"] = payload["table_details"]

#         _SCHEMA_CACHE["last_loaded"] = time.time()
#     except Exception:
#         return


# def get_table_registry(force_refresh: bool = False) -> List[Dict[str, Any]]:
#     with _CACHE_LOCK:
#         now = time.time()

#         if (
#             not force_refresh
#             and _SCHEMA_CACHE["table_registry"] is not None
#             and now - _SCHEMA_CACHE["last_loaded"] < _SCHEMA_CACHE["ttl_seconds"]
#         ):
#             return _SCHEMA_CACHE["table_registry"]

#         if not force_refresh and _SCHEMA_CACHE["table_registry"] is None:
#             _read_cache_from_disk()
#             if _SCHEMA_CACHE["table_registry"] is not None:
#                 return _SCHEMA_CACHE["table_registry"]

#         registry = _build_table_registry()
#         _SCHEMA_CACHE["table_registry"] = registry
#         _SCHEMA_CACHE["last_loaded"] = now
#         _write_cache_to_disk()
#         return registry


# def get_semantic_catalog(force_refresh: bool = False) -> Dict[str, Dict[str, str]]:
#     with _CACHE_LOCK:
#         now = time.time()

#         if (
#             not force_refresh
#             and _SCHEMA_CACHE["semantic_catalog"] is not None
#             and now - _SCHEMA_CACHE["last_loaded"] < _SCHEMA_CACHE["ttl_seconds"]
#         ):
#             return _SCHEMA_CACHE["semantic_catalog"]

#         if not force_refresh and _SCHEMA_CACHE["semantic_catalog"] is None:
#             _read_cache_from_disk()
#             if _SCHEMA_CACHE["semantic_catalog"] is not None:
#                 return _SCHEMA_CACHE["semantic_catalog"]

#         registry = get_table_registry(force_refresh=force_refresh)
#         catalog = _build_semantic_catalog(registry)
#         _SCHEMA_CACHE["semantic_catalog"] = catalog
#         _SCHEMA_CACHE["last_loaded"] = now
#         _write_cache_to_disk()
#         return catalog


# def _friendly_semantics(column_name: str, catalog: Dict[str, Dict[str, str]]) -> Dict[str, str]:
#     upper = str(column_name).upper()
#     meta = catalog.get(upper, {"label": "", "topic": "", "universe": ""})

#     # Minimal geography semantics
#     if upper == "CENSUS_BLOCK_GROUP":
#         return {"label": "census block group", "topic": "geography", "universe": "census block group"}

#     return meta


# def get_table_details(table_name: str, force_refresh: bool = False) -> Dict[str, Any]:
#     with _CACHE_LOCK:
#         if not force_refresh and table_name in _SCHEMA_CACHE["table_details"]:
#             return _SCHEMA_CACHE["table_details"][table_name]

#         registry = get_table_registry(force_refresh=False)
#         registry_map = {item["table_name"]: item for item in registry}
#         table_meta = registry_map.get(table_name)

#         if table_meta is None:
#             raise ValueError(f"Unknown table: {table_name}")

#         catalog = get_semantic_catalog(force_refresh=False)
#         raw_columns = _get_table_columns(table_name)
#         join_keys = _detect_join_keys(raw_columns)

#         columns: List[Dict[str, Any]] = []
#         for col in raw_columns:
#             col_name = str(col.get("name") or "")
#             semantic = _friendly_semantics(col_name, catalog)

#             columns.append(
#                 {
#                     **col,
#                     "label": semantic.get("label", ""),
#                     "topic": semantic.get("topic", ""),
#                     "universe": semantic.get("universe", ""),
#                 }
#             )

#         detail = {
#             **table_meta,
#             "join_keys": join_keys,
#             "columns": columns,
#         }

#         _SCHEMA_CACHE["table_details"][table_name] = detail
#         _write_cache_to_disk()
#         return detail


# def get_schema_text_summary(max_tables: int = 10, max_columns: int = 40) -> str:
#     registry = get_table_registry(force_refresh=False)
#     blocks: List[str] = []

#     for item in registry[:max_tables]:
#         detail = get_table_details(item["table_name"], force_refresh=False)
#         lines: List[str] = []

#         for col in detail["columns"][:max_columns]:
#             name = col.get("name")
#             col_type = col.get("type")
#             label = col.get("label", "")
#             topic = col.get("topic", "")
#             universe = col.get("universe", "")
#             semantic_parts = [x for x in [label, topic, universe] if x]

#             if semantic_parts:
#                 lines.append(f"{name} ({col_type}) -- " + " | ".join(semantic_parts))
#             else:
#                 lines.append(f"{name} ({col_type})")

#         header = f'Table: "{detail["table_name"]}"'
#         if detail.get("year") is not None:
#             header += f' | Year: {detail["year"]}'
#         if detail.get("table_kind"):
#             header += f' | Kind: {detail["table_kind"]}'
#         if detail.get("join_keys"):
#             header += f' | Join keys: {", ".join(detail["join_keys"])}'

#         blocks.append(header + "\nColumns:\n" + "\n".join(lines))

#     return "\n\n".join(blocks)


# def warm_schema_cache() -> None:
#     get_table_registry(force_refresh=False)
#     get_semantic_catalog(force_refresh=False)


# def refresh_schema_cache() -> None:
#     get_table_registry(force_refresh=True)
#     get_semantic_catalog(force_refresh=True)


import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.db import run_query


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

SCHEMA_CACHE_FILE = CACHE_DIR / "schema_metadata.json"
_CACHE_LOCK = threading.RLock()

_SCHEMA_CACHE: Dict[str, Any] = {
    "registry": None,
    "semantic_catalog": None,
    "details": {},
    "last_loaded": 0,
    "ttl_seconds": 3600,
}

JOIN_KEYS = ["CENSUS_BLOCK_GROUP", "GEOID", "GEOID20", "GEOID10"]


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_parts(name: str) -> List[str]:
    return [p for p in str(name).upper().split("_") if p]


def _extract_year(name: str) -> Optional[int]:
    for part in _table_parts(name):
        if len(part) == 4 and part.isdigit():
            return int(part)
    return None


def _extract_group_code(name: str) -> str:
    for part in reversed(_table_parts(name)):
        if re.match(r"^[A-Z]\d+$", part):
            return part
    return ""


def _extract_bucket(code: str) -> str:
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    return digits[:2] if digits else ""


def _kind(name: str, object_kind: str) -> str:
    upper = str(name).upper()
    if object_kind == "view":
        return "view"
    if "FIELD_DESCRIPTIONS" in upper:
        return "dictionary"
    if "FIPS_CODES" in upper:
        return "fips"
    if "GEOGRAPHIC_DATA" in upper:
        return "geography"
    if "GEOMETRY" in upper:
        return "geometry"
    if "PATTERNS" in upper:
        return "patterns"
    return "fact"


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).upper(): v for k, v in row.items()}


def _show_objects() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for row in run_query("SHOW TABLES;"):
        name = row.get("name")
        if name:
            out.append({"name": str(name), "object_kind": "table"})

    try:
        for row in run_query("SHOW VIEWS;"):
            name = row.get("name")
            if name:
                out.append({"name": str(name), "object_kind": "view"})
    except Exception:
        pass

    return out


def _describe_object(name: str, object_kind: str) -> List[Dict[str, Any]]:
    stmt = "VIEW" if object_kind == "view" else "TABLE"
    rows = run_query(f'DESCRIBE {stmt} {_quote_ident(name)};')

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


def _detect_join_keys(columns: List[Dict[str, Any]]) -> List[str]:
    names = {str(c.get("name") or "").upper() for c in columns}
    return [k for k in JOIN_KEYS if k in names]


def _build_registry() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    for obj in _show_objects():
        name = obj["name"]
        object_kind = obj["object_kind"]
        group_code = _extract_group_code(name)

        items.append(
            {
                "table_name": name,
                "object_kind": object_kind,
                "table_kind": _kind(name, object_kind),
                "year": _extract_year(name),
                "group_code": group_code,
                "group_bucket": _extract_bucket(group_code),
            }
        )

    items.sort(key=lambda x: ((x.get("year") or 0), x["table_name"]), reverse=True)
    return items


def _build_semantic_catalog(registry: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    catalog: Dict[str, Dict[str, str]] = {}

    for item in registry:
        if item["table_kind"] != "dictionary":
            continue

        try:
            rows = run_query(f'SELECT * FROM {_quote_ident(item["table_name"])}')
        except Exception:
            continue

        for raw_row in rows:
            row = _normalize_row(raw_row)
            column_id = str(row.get("COLUMN_ID") or "").strip().upper()
            if not column_id:
                continue

            label = str(row.get("FIELD_NAME") or "").strip()
            topic = str(row.get("COLUMN_TOPIC") or "").strip()
            universe = str(row.get("COLUMN_UNIVERSE") or "").strip()

            current = catalog.get(column_id, {"label": "", "topic": "", "universe": ""})
            if label and not current["label"]:
                current["label"] = label
            if topic and not current["topic"]:
                current["topic"] = topic
            if universe and not current["universe"]:
                current["universe"] = universe

            catalog[column_id] = current

    return catalog


def _read_cache() -> None:
    if not SCHEMA_CACHE_FILE.exists():
        return

    try:
        payload = json.loads(SCHEMA_CACHE_FILE.read_text())
        if isinstance(payload.get("registry"), list):
            _SCHEMA_CACHE["registry"] = payload["registry"]
        if isinstance(payload.get("semantic_catalog"), dict):
            _SCHEMA_CACHE["semantic_catalog"] = payload["semantic_catalog"]
        if isinstance(payload.get("details"), dict):
            _SCHEMA_CACHE["details"] = payload["details"]
        _SCHEMA_CACHE["last_loaded"] = time.time()
    except Exception:
        return


def _write_cache() -> None:
    payload = {
        "saved_at": time.time(),
        "registry": _SCHEMA_CACHE["registry"],
        "semantic_catalog": _SCHEMA_CACHE["semantic_catalog"],
        "details": _SCHEMA_CACHE["details"],
    }
    SCHEMA_CACHE_FILE.write_text(json.dumps(payload, indent=2))


def get_table_registry(force_refresh: bool = False) -> List[Dict[str, Any]]:
    with _CACHE_LOCK:
        now = time.time()

        if (
            not force_refresh
            and _SCHEMA_CACHE["registry"] is not None
            and now - _SCHEMA_CACHE["last_loaded"] < _SCHEMA_CACHE["ttl_seconds"]
        ):
            return _SCHEMA_CACHE["registry"]

        if not force_refresh and _SCHEMA_CACHE["registry"] is None:
            _read_cache()
            if _SCHEMA_CACHE["registry"] is not None:
                return _SCHEMA_CACHE["registry"]

        _SCHEMA_CACHE["registry"] = _build_registry()
        _SCHEMA_CACHE["last_loaded"] = now
        _write_cache()
        return _SCHEMA_CACHE["registry"]


def get_semantic_catalog(force_refresh: bool = False) -> Dict[str, Dict[str, str]]:
    with _CACHE_LOCK:
        now = time.time()

        if (
            not force_refresh
            and _SCHEMA_CACHE["semantic_catalog"] is not None
            and now - _SCHEMA_CACHE["last_loaded"] < _SCHEMA_CACHE["ttl_seconds"]
        ):
            return _SCHEMA_CACHE["semantic_catalog"]

        if not force_refresh and _SCHEMA_CACHE["semantic_catalog"] is None:
            _read_cache()
            if _SCHEMA_CACHE["semantic_catalog"] is not None:
                return _SCHEMA_CACHE["semantic_catalog"]

        registry = get_table_registry(force_refresh=force_refresh)
        _SCHEMA_CACHE["semantic_catalog"] = _build_semantic_catalog(registry)
        _SCHEMA_CACHE["last_loaded"] = now
        _write_cache()
        return _SCHEMA_CACHE["semantic_catalog"]


def get_table_details(table_name: str, force_refresh: bool = False) -> Dict[str, Any]:
    with _CACHE_LOCK:
        if not force_refresh and table_name in _SCHEMA_CACHE["details"]:
            return _SCHEMA_CACHE["details"][table_name]

        registry = get_table_registry(force_refresh=False)
        item = next((x for x in registry if x["table_name"] == table_name), None)
        if item is None:
            raise ValueError(f"Unknown object: {table_name}")

        catalog = get_semantic_catalog(force_refresh=False)
        raw_columns = _describe_object(table_name, item["object_kind"])
        join_keys = _detect_join_keys(raw_columns)

        columns: List[Dict[str, Any]] = []
        for col in raw_columns:
            name = str(col.get("name") or "")
            meta = catalog.get(name.upper(), {"label": "", "topic": "", "universe": ""})

            if name.upper() == "CENSUS_BLOCK_GROUP":
                if not meta.get("label"):
                    meta["label"] = "Census block group"
                if not meta.get("topic"):
                    meta["topic"] = "Geography"
                if not meta.get("universe"):
                    meta["universe"] = "Census block group"

            columns.append(
                {
                    **col,
                    "label": meta.get("label", ""),
                    "topic": meta.get("topic", ""),
                    "universe": meta.get("universe", ""),
                }
            )

        detail = {**item, "join_keys": join_keys, "columns": columns}
        _SCHEMA_CACHE["details"][table_name] = detail
        _write_cache()
        return detail


def get_schema_text_summary(max_tables: int = 8, max_columns: int = 25) -> str:
    blocks: List[str] = []

    for item in get_table_registry(force_refresh=False)[:max_tables]:
        detail = get_table_details(item["table_name"], force_refresh=False)
        lines: List[str] = []

        for col in detail["columns"][:max_columns]:
            parts = [x for x in [col.get("label", ""), col.get("topic", ""), col.get("universe", "")] if x]
            if parts:
                lines.append(f'{col["name"]} ({col["type"]}) -- ' + " | ".join(parts))
            else:
                lines.append(f'{col["name"]} ({col["type"]})')

        header = f'Table: "{detail["table_name"]}"'
        if detail.get("year") is not None:
            header += f' | Year: {detail["year"]}'
        header += f' | Kind: {detail["table_kind"]}'
        if detail.get("join_keys"):
            header += f' | Join keys: {", ".join(detail["join_keys"])}'

        blocks.append(header + "\nColumns:\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def warm_schema_cache() -> None:
    get_table_registry(force_refresh=False)
    get_semantic_catalog(force_refresh=False)


def refresh_schema_cache() -> None:
    get_table_registry(force_refresh=True)
    get_semantic_catalog(force_refresh=True)