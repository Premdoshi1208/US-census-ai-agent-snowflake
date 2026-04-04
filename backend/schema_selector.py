import re
from typing import Dict, List, Tuple

from backend.schema_service import get_schema_metadata


DOMAIN_HINTS = {
    "population": ["population", "people", "person", "male", "female", "sex", "age", "demographic", "total"],
    "income": ["income", "median", "household", "earnings", "salary", "poverty"],
    "rent": ["rent", "renter", "gross", "housing", "tenure", "occupancy"],
    "household": ["household", "households", "family", "families"],
    "compare": ["compare", "comparison", "change", "difference", "trend", "year", "growth"],
}

GEO_TOKENS = {
    "state",
    "states",
    "county",
    "counties",
    "city",
    "area",
    "areas",
    "region",
    "regions",
    "geography",
    "top",
    "bottom",
}

LOCATION_COLUMNS = [
    "STATE_NAME",
    "STATE",
    "STATEFP",
    "COUNTY_NAME",
    "COUNTY",
    "COUNTYFP",
    "TRACTCE",
    "NAME",
    "GEOID",
    "GEOID20",
    "CENSUS_BLOCK_GROUP",
]


def _normalize_token(token: str) -> str:
    token = token.lower().strip()
    if len(token) > 4 and token.endswith("s"):
        token = token[:-1]
    return token


def _tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+", text.lower())
    return [_normalize_token(t) for t in tokens if len(t) >= 2]


def _expand_query_terms(query: str) -> List[str]:
    tokens = set(_tokenize(query))

    for trigger, expansions in DOMAIN_HINTS.items():
        if trigger in tokens:
            tokens.update(expansions)

    if "people" in tokens:
        tokens.add("population")
    if "salary" in tokens:
        tokens.add("income")
    if "renter" in tokens:
        tokens.add("rent")

    return sorted(tokens)


def _years_in_query(query: str) -> List[int]:
    return [int(y) for y in re.findall(r"\b(20\d{2})\b", query)]


def _score_text(value: str, query_terms: List[str], strong: int, weak: int) -> int:
    if not value:
        return 0

    text = str(value).lower()
    score = 0
    for term in query_terms:
        if len(term) >= 5 and term in text:
            score += strong
        elif term in text:
            score += weak
    return score


def _score_column(col: Dict, query_terms: List[str]) -> int:
    name = str(col.get("name", "") or "")
    label = str(col.get("label", "") or "")
    concept = str(col.get("concept", "") or "")
    group_code = str(col.get("group_code", "") or "")

    score = 0
    score += _score_text(label, query_terms, strong=18, weak=7)
    score += _score_text(concept, query_terms, strong=14, weak=5)
    score += _score_text(name, query_terms, strong=8, weak=3)
    score += _score_text(group_code, query_terms, strong=5, weak=2)
    return score


def _query_mentions_geography(user_query: str) -> bool:
    lower = user_query.lower()
    return any(token in lower for token in GEO_TOKENS)


def _choose_columns(table: Dict, scored_columns: List[Tuple[int, Dict]], max_columns: int) -> List[Dict]:
    chosen: List[Dict] = []
    seen = set()

    for join_key in table.get("join_keys", []):
        for col in table.get("columns", []):
            if str(col.get("name") or "").upper() == join_key and join_key not in seen:
                chosen.append(col)
                seen.add(join_key)

    for loc_name in LOCATION_COLUMNS:
        for col in table.get("columns", []):
            if str(col.get("name") or "").upper() == loc_name and loc_name not in seen:
                chosen.append(col)
                seen.add(loc_name)

    for _, col in scored_columns:
        name = str(col.get("name") or "")
        upper = name.upper()
        if not name or upper in seen:
            continue
        chosen.append(col)
        seen.add(upper)
        if len(chosen) >= max_columns:
            return chosen

    for col in table.get("columns", []):
        name = str(col.get("name") or "")
        upper = name.upper()
        if not name or upper in seen:
            continue
        chosen.append(col)
        seen.add(upper)
        if len(chosen) >= max_columns:
            break

    return chosen


def get_relevant_schema_bundle(
    user_query: str,
    max_tables: int = 8,
    max_columns: int = 30,
) -> Dict:
    schema = get_schema_metadata()
    query_terms = _expand_query_terms(user_query)
    requested_years = _years_in_query(user_query)
    wants_geography = _query_mentions_geography(user_query)

    ranked_tables: List[Tuple[int, Dict, List[Tuple[int, Dict]]]] = []

    for table in schema:
        table_name = str(table.get("table_name") or "")
        year = table.get("year")
        table_kind = str(table.get("table_kind") or "")
        columns = table.get("columns", [])

        table_name_score = _score_text(table_name, query_terms, strong=10, weak=4)

        scored_columns: List[Tuple[int, Dict]] = []
        for col in columns:
            score = _score_column(col, query_terms)
            if score > 0:
                scored_columns.append((score, col))

        scored_columns.sort(key=lambda x: x[0], reverse=True)

        table_score = table_name_score + sum(score for score, _ in scored_columns[:10])

        if requested_years:
            if year in requested_years:
                table_score += 25
        else:
            if year is not None:
                table_score += max(0, year - 2018)

        if table_kind == "fact":
            table_score += 12
        elif table_kind == "geography" and wants_geography:
            table_score += 35
        elif table_kind == "geometry":
            table_score -= 35
        elif table_kind == "patterns":
            table_score -= 20

        ranked_tables.append((table_score, table, scored_columns))

    ranked_tables.sort(key=lambda x: x[0], reverse=True)
    best_score = ranked_tables[0][0] if ranked_tables else 0

    selected: List[Tuple[int, Dict, List[Tuple[int, Dict]]]] = []

    if requested_years:
        picked_fact_years = set()

        for score, table, scored_columns in ranked_tables:
            year = table.get("year")
            table_kind = table.get("table_kind")

            if year in requested_years:
                selected.append((score, table, scored_columns))
                if table_kind == "fact":
                    picked_fact_years.add(year)

            if len(picked_fact_years) == len(set(requested_years)):
                break
    else:
        selected = ranked_tables[:max_tables]

    if wants_geography and not any(item[1].get("table_kind") == "geography" for item in selected):
        for score, table, scored_columns in ranked_tables:
            if table.get("table_kind") == "geography":
                selected.insert(0, (score, table, scored_columns))
                break

    unique_selected: List[Tuple[int, Dict, List[Tuple[int, Dict]]]] = []
    seen_names = set()
    for item in selected:
        table_name = item[1]["table_name"]
        if table_name in seen_names:
            continue
        unique_selected.append(item)
        seen_names.add(table_name)
        if len(unique_selected) >= max_tables:
            break

    selected_table_names: List[str] = []
    selected_table_details: List[Dict] = []
    schema_blocks: List[str] = []

    for _, table, scored_columns in unique_selected:
        table_name = table["table_name"]
        chosen_columns = _choose_columns(table, scored_columns, max_columns=max_columns)

        selected_table_names.append(table_name)
        selected_table_details.append(
            {
                "table_name": table_name,
                "year": table.get("year"),
                "table_kind": table.get("table_kind"),
                "join_keys": table.get("join_keys", []),
                "columns": chosen_columns,
            }
        )

        lines: List[str] = []
        for col in chosen_columns:
            name = col.get("name")
            col_type = col.get("type")
            label = col.get("label", "")
            concept = col.get("concept", "")
            group_code = col.get("group_code", "")

            if not name or not col_type:
                continue

            semantic_parts = [x for x in [label, concept, group_code] if x]
            if semantic_parts:
                lines.append(f"{name} ({col_type}) -- " + " | ".join(semantic_parts))
            else:
                lines.append(f"{name} ({col_type})")

        header = f'Table: "{table_name}"'
        if table.get("year") is not None:
            header += f' | Year: {table["year"]}'
        if table.get("table_kind"):
            header += f' | Kind: {table["table_kind"]}'
        if table.get("join_keys"):
            header += f' | Join keys: {", ".join(table["join_keys"])}'

        schema_blocks.append(header + "\nColumns:\n" + "\n".join(lines))

    off_topic = best_score <= 0 and not any(
        word in user_query.lower()
        for word in [
            "population",
            "income",
            "rent",
            "household",
            "housing",
            "male",
            "female",
            "compare",
            "year",
            "growth",
            "state",
            "county",
        ]
    )

    return {
        "off_topic": off_topic,
        "best_score": best_score,
        "selected_table_names": selected_table_names,
        "selected_table_details": selected_table_details,
        "schema_text": "\n\n".join(schema_blocks),
    }