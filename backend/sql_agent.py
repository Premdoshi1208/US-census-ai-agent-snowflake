import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.db import run_query
from backend.llm import LLMRateLimitError, LLMServiceError, call_llm
from backend.schema_service import get_table_details, get_table_registry


ENABLE_LLM_FALLBACK = os.getenv("ENABLE_LLM_FALLBACK", "true").strip().lower() == "true"
AVAILABLE_YEARS = {2019, 2020}

STOPWORDS = {
    "what", "is", "the", "a", "an", "show", "me", "of", "for", "by", "between", "and",
    "from", "to", "in", "on", "related", "statistics", "statistic", "give", "top", "bottom",
    "compare", "growth", "rate", "total", "all", "year", "years", "across", "over",
    "historical", "history", "precise", "with", "within",
}

MEASURE_TYPE_PATTERNS = ("NUMBER", "INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC")

KNOWN_CODE_BOOSTS = {
    "population": {"P0010001", "B01001E1", "B01003E1"},
    "female_population": {"B01001E26"},
    "male_population": {"B01001E2"},
    "income": {"B19013E1"},
    "rent": {"B25064E1", "B25058E1"},
}

STATE_FIPS_TO_NAME = {
    "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas", "06": "California",
    "08": "Colorado", "09": "Connecticut", "10": "Delaware", "11": "District of Columbia",
    "12": "Florida", "13": "Georgia", "15": "Hawaii", "16": "Idaho", "17": "Illinois",
    "18": "Indiana", "19": "Iowa", "20": "Kansas", "21": "Kentucky", "22": "Louisiana",
    "23": "Maine", "24": "Maryland", "25": "Massachusetts", "26": "Michigan", "27": "Minnesota",
    "28": "Mississippi", "29": "Missouri", "30": "Montana", "31": "Nebraska", "32": "Nevada",
    "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico", "36": "New York",
    "37": "North Carolina", "38": "North Dakota", "39": "Ohio", "40": "Oklahoma", "41": "Oregon",
    "42": "Pennsylvania", "44": "Rhode Island", "45": "South Carolina", "46": "South Dakota",
    "47": "Tennessee", "48": "Texas", "49": "Utah", "50": "Vermont", "51": "Virginia",
    "53": "Washington", "54": "West Virginia", "55": "Wisconsin", "56": "Wyoming",
}
STATE_NAME_TO_FIPS = {v.lower(): k for k, v in STATE_FIPS_TO_NAME.items()}


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _normalize_token(token: str) -> str:
    token = token.lower().strip()
    if len(token) > 4 and token.endswith("s"):
        token = token[:-1]
    return token


def _tokenize(text: str) -> List[str]:
    return [_normalize_token(t) for t in re.findall(r"[A-Za-z0-9_]+", text.lower()) if len(t) >= 2]


def _query_terms(query: str) -> List[str]:
    return [t for t in _tokenize(query) if t not in STOPWORDS]


def _years_in_query(query: str) -> List[int]:
    return [int(y) for y in re.findall(r"\b(20\d{2})\b", query)]


def _unavailable_years_in_query(query: str) -> List[int]:
    years = _years_in_query(query)
    return [y for y in years if y not in AVAILABLE_YEARS]


def _score_text(value: str, terms: List[str], strong: int, weak: int) -> int:
    if not value:
        return 0
    text = str(value).lower()
    score = 0
    for term in terms:
        if len(term) >= 5 and term in text:
            score += strong
        elif term in text:
            score += weak
    return score


def _query_metric_family(query: str) -> str:
    q = query.lower()

    if "female" in q or "females" in q or "women" in q:
        return "female_population"
    if "male" in q or "males" in q or "men" in q:
        return "male_population"
    if "income" in q:
        return "income"
    if "rent" in q:
        return "rent"
    if "household" in q:
        return "household"
    if "population" in q or "people" in q:
        return "population"

    return "generic"


def _is_numeric_type(type_name: str) -> bool:
    upper = str(type_name or "").upper()
    return any(p in upper for p in MEASURE_TYPE_PATTERNS)


def _is_measure_column(col: Dict[str, Any], join_keys: List[str]) -> bool:
    name = str(col.get("name") or "").upper()
    return name not in {str(x).upper() for x in join_keys} and _is_numeric_type(col.get("type"))


def _friendly_metric_name(query: str, candidate: Optional[Dict[str, Any]] = None) -> str:
    family = _query_metric_family(query)

    if family == "female_population":
        return "female population"
    if family == "male_population":
        return "male population"
    if family == "population":
        return "total population"

    if candidate:
        label = str(candidate.get("column_label") or "").strip()
        universe = str(candidate.get("column_universe") or "").strip()

        if label and universe:
            if label.lower() == "total":
                return universe.lower()
            return f"{label.lower()} ({universe.lower()})"
        if label:
            return label.lower()

    if family == "income":
        return "median household income"
    if family == "rent":
        return "rent"
    return "value"


def _resolved_year_phrase(query: str, fallback_year: Optional[int]) -> str:
    years = _years_in_query(query)
    if years:
        return ""
    if fallback_year is not None:
        return f"Using {fallback_year} data, "
    return ""


def _sql_alias_from_name(name: str) -> str:
    alias = re.sub(r"[^A-Za-z0-9]+", "_", name.strip().upper())
    alias = re.sub(r"_+", "_", alias).strip("_")
    return alias or "VALUE"


def _candidate_objects(query: str, year: Optional[int] = None, limit: int = 10) -> List[str]:
    registry = get_table_registry(force_refresh=False)
    family = _query_metric_family(query)
    scored: List[Tuple[int, str]] = []

    for item in registry:
        name = item["table_name"]
        kind = item["table_kind"]
        item_year = item.get("year")
        bucket = str(item.get("group_bucket") or "")
        upper = name.upper()

        if year is not None and item_year != year:
            continue

        score = 0

        if year is None and item_year is not None:
            score += max(0, item_year - 2018)

        if kind == "view":
            if family == "rent" and "RENT" in upper:
                score += 120
            if family == "income" and "INCOME" in upper:
                score += 60
            if "GEO" in upper:
                score += 20

        elif kind == "fact":
            if family in {"population", "female_population", "male_population"}:
                if "REDISTRICTING_CBG_DATA" in upper:
                    score += 90
                if bucket == "01":
                    score += 70
            elif family == "income":
                if bucket == "19":
                    score += 95
            elif family == "rent":
                if bucket == "25":
                    score += 95
            elif family == "household":
                if bucket == "11":
                    score += 60

        if kind in {"geometry", "patterns", "dictionary"}:
            score -= 100

        if score > 0:
            scored.append((score, name))

    scored.sort(key=lambda x: x[0], reverse=True)

    out: List[str] = []
    for _, name in scored:
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _metric_score(col: Dict[str, Any], query: str) -> int:
    terms = _query_terms(query)
    family = _query_metric_family(query)

    name = str(col.get("name", "") or "")
    upper = name.upper()
    label = str(col.get("label", "") or "")
    topic = str(col.get("topic", "") or "")
    universe = str(col.get("universe", "") or "")

    score = 0
    score += _score_text(label, terms, 18, 7)
    score += _score_text(topic, terms, 12, 5)
    score += _score_text(universe, terms, 16, 6)
    score += _score_text(name, terms, 8, 3)

    semantic = f"{name} {label} {topic} {universe}".lower()

    if family == "population":
        if "total population" in universe.lower():
            score += 120
        if label.lower() == "total":
            score += 60
        if upper in KNOWN_CODE_BOOSTS["population"]:
            score += 150

    elif family == "female_population":
        if "female" in semantic:
            score += 130
        if upper in KNOWN_CODE_BOOSTS["female_population"]:
            score += 180

    elif family == "male_population":
        if "male" in semantic:
            score += 130
        if upper in KNOWN_CODE_BOOSTS["male_population"]:
            score += 180

    elif family == "income":
        if "income" in semantic:
            score += 120
        if "median" in semantic:
            score += 50
        if upper in KNOWN_CODE_BOOSTS["income"]:
            score += 180

    elif family == "rent":
        if "rent" in semantic:
            score += 120
        if "median" in semantic:
            score += 50
        if upper in KNOWN_CODE_BOOSTS["rent"]:
            score += 180

    return score


def _resolve_metric_candidate(query: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    best = None

    for name in _candidate_objects(query, year=year, limit=10):
        detail = get_table_details(name, force_refresh=False)
        join_keys = detail.get("join_keys", [])

        for col in detail["columns"]:
            if not _is_measure_column(col, join_keys):
                continue

            score = _metric_score(col, query)
            if score <= 0:
                continue

            candidate = {
                "score": score,
                "table_name": name,
                "year": detail.get("year"),
                "column_name": col["name"],
                "column_label": col.get("label", ""),
                "column_topic": col.get("topic", ""),
                "column_universe": col.get("universe", ""),
                "table_detail": detail,
            }

            if best is None or candidate["score"] > best["score"]:
                best = candidate

    return best


def _extract_state_filter_code(query: str) -> Optional[str]:
    q = query.lower()
    for state_name, state_code in STATE_NAME_TO_FIPS.items():
        if state_name in q:
            return state_code
    return None


def _cbg_expr(column: str = "CENSUS_BLOCK_GROUP") -> str:
    return f"CAST({_quote_ident(column)} AS VARCHAR)"


def _group_target(query: str) -> str:
    q = query.lower()
    if "state" in q:
        return "state"
    if "county" in q or "counties" in q:
        return "county"
    return "area"


def _rank_n(query: str) -> int:
    match = re.search(r"\b(top|bottom)\s+(\d+)\b", query.lower())
    return int(match.group(2)) if match else 10


def _is_compare_query(query: str) -> bool:
    q = query.lower()
    years = _years_in_query(query)
    return len(years) >= 2 and ("compare" in q or "between" in q or "growth" in q or "change" in q or "from" in q)


def _is_growth_query(query: str) -> bool:
    q = query.lower()
    return "growth" in q or "percent change" in q or "change rate" in q


def _is_ranking_query(query: str) -> bool:
    q = query.lower()
    return "top " in q or "bottom " in q


def _looks_like_contextual_followup(query: str) -> bool:
    q = query.lower().strip()
    return bool(re.fullmatch(r"(and\s+)?(for|in|of)\s+20\d{2}\??", q))


def _off_topic(query: str) -> bool:
    q = query.lower()

    if _looks_like_contextual_followup(query):
        return False

    return not any(
        word in q
        for word in [
            "population", "income", "rent", "household", "housing",
            "female", "females", "male", "males", "women", "men",
            "state", "county", "counties", "area", "census",
            "year", "growth", "compare", "change", "land", "water",
            "latitude", "longitude", "2019", "2020",
        ]
    )


def _state_filter_where_clause(state_code: Optional[str]) -> str:
    return f"WHERE SUBSTR({_cbg_expr()}, 1, 2) = '{state_code}'" if state_code else ""


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return str(value)


def _lookup_geo_names(rows: List[Dict[str, Any]], year: Optional[int]) -> List[Dict[str, Any]]:
    if not rows:
        return rows

    county_codes: List[str] = []
    for row in rows:
        if row.get("STATE_FIPS") is not None:
            sf = str(row["STATE_FIPS"]).zfill(2)
            row["STATE_FIPS"] = sf
            row["STATE_NAME"] = STATE_FIPS_TO_NAME.get(sf, sf)

        if row.get("COUNTY_FIPS") is not None:
            county_codes.append(str(row["COUNTY_FIPS"]).zfill(5))

    county_codes = sorted(set(county_codes))
    if not county_codes:
        return rows

    registry = get_table_registry(force_refresh=False)
    fips_table = None
    for item in registry:
        if item["table_kind"] == "fips" and (year is None or item.get("year") == year):
            fips_table = item["table_name"]
            break

    if fips_table is None:
        return rows

    fips_detail = get_table_details(fips_table, force_refresh=False)
    available_cols = {str(col["name"]).upper() for col in fips_detail["columns"]}

    state_col = "STATE" if "STATE" in available_cols else None
    county_col = "COUNTY" if "COUNTY" in available_cols else None
    state_fips_col = "STATE_FIPS" if "STATE_FIPS" in available_cols else None
    county_fips_col = "COUNTY_FIPS" if "COUNTY_FIPS" in available_cols else None

    if not state_fips_col or not county_fips_col:
        return rows

    select_parts = [
        f"LPAD(CAST({_quote_ident(state_fips_col)} AS VARCHAR), 2, '0') || "
        f"LPAD(CAST({_quote_ident(county_fips_col)} AS VARCHAR), 3, '0') AS COUNTY_FIPS"
    ]
    if state_col:
        select_parts.append(_quote_ident(state_col))
    if county_col:
        select_parts.append(_quote_ident(county_col))

    in_list = ",".join(f"'{code}'" for code in county_codes)
    lookup_sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {_quote_ident(fips_table)} "
        f"WHERE LPAD(CAST({_quote_ident(state_fips_col)} AS VARCHAR), 2, '0') || "
        f"LPAD(CAST({_quote_ident(county_fips_col)} AS VARCHAR), 3, '0') IN ({in_list})"
    )

    mapping: Dict[str, Dict[str, Any]] = {}
    try:
        for result in run_query(lookup_sql):
            mapping[str(result["COUNTY_FIPS"]).zfill(5)] = {
                "STATE": result.get("STATE") if state_col else None,
                "COUNTY": result.get("COUNTY") if county_col else None,
            }
    except Exception:
        return rows

    for row in rows:
        if row.get("COUNTY_FIPS") is not None:
            cf = str(row["COUNTY_FIPS"]).zfill(5)
            row["COUNTY_FIPS"] = cf
            meta = mapping.get(cf)
            if meta:
                if meta.get("COUNTY"):
                    row["COUNTY_NAME"] = meta.get("COUNTY")
                if not row.get("STATE_NAME") and meta.get("STATE"):
                    row["STATE_ABBR"] = meta.get("STATE")

    return rows


def _single_row_summary(row: Dict[str, Any]) -> str:
    items = [(k, v) for k, v in row.items() if v is not None]
    if not items:
        return "The query returned no non-null values."
    if len(items) == 1:
        key, value = items[0]
        return f'The {str(key).replace("_", " ").lower()} is {_format_value(value)}.'
    return "I found one row: " + "; ".join(
        f'{str(k).replace("_", " ").lower()} = {_format_value(v)}' for k, v in items[:5]
    ) + "."


def _default_chart_hint(query: str) -> str:
    q = query.lower()
    if _is_compare_query(query) or _is_ranking_query(query):
        return "bar"
    if "trend" in q or "growth" in q:
        return "line"
    return "table"


def _run_sql_answer(
    question: str,
    sql: str,
    answer_builder=None,
    selected_tables: Optional[List[str]] = None,
    chart_hint: str = "table",
    year: Optional[int] = None,
    postprocess_geo: bool = False,
) -> Dict[str, Any]:
    rows = run_query(sql)
    if postprocess_geo:
        rows = _lookup_geo_names(rows, year=year)

    if answer_builder is None:
        if rows:
            answer = _single_row_summary(rows[0]) if len(rows) == 1 else f"I found {len(rows)} rows."
        else:
            answer = "The query ran successfully but returned no rows."
    else:
        answer = answer_builder(rows)

    return {
        "status": "ok",
        "question": question,
        "answer": answer,
        "sql": sql,
        "result": rows,
        "row_count": len(rows),
        "error": None,
        "selected_tables": selected_tables or [],
        "chart_hint": chart_hint,
    }


def _answer_total_metric(query: str) -> Optional[Dict[str, Any]]:
    years = _years_in_query(query)
    year = years[0] if years else None
    candidate = _resolve_metric_candidate(query, year=year)
    if candidate is None:
        return None

    metric_name = _friendly_metric_name(query, candidate)
    metric_alias = _sql_alias_from_name(metric_name)
    family = _query_metric_family(query)
    state_filter = _extract_state_filter_code(query)
    where_clause = _state_filter_where_clause(state_filter)
    agg = "MEDIAN" if family in {"income", "rent"} else "SUM"

    sql = (
        f"SELECT {agg}({_quote_ident(candidate['column_name'])}) AS {_quote_ident(metric_alias)} "
        f"FROM {_quote_ident(candidate['table_name'])} {where_clause}"
    ).strip()

    def _builder(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return "The query ran successfully but returned no rows."
        value = rows[0].get(metric_alias)
        if value is None:
            value = rows[0].get(metric_alias.upper())
        scope = f" in {STATE_FIPS_TO_NAME.get(state_filter, state_filter)}" if state_filter else ""
        prefix = _resolved_year_phrase(query, candidate.get("year"))
        return f"{prefix}the {metric_name}{scope} is {_format_value(value)}."

    return _run_sql_answer(
        question=query,
        sql=sql,
        answer_builder=_builder,
        selected_tables=[candidate["table_name"]],
        chart_hint="table",
    )


def _answer_compare_years(query: str) -> Optional[Dict[str, Any]]:
    years = _years_in_query(query)
    if len(years) < 2:
        return None

    year_a, year_b = years[0], years[1]
    cand_a = _resolve_metric_candidate(query, year=year_a)
    cand_b = _resolve_metric_candidate(query, year=year_b)

    if cand_a is None or cand_b is None:
        return {
            "status": "unanswerable",
            "question": query,
            "answer": f"I could not resolve the requested metric for both {year_a} and {year_b} from the available tables.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [x for x in [cand_a["table_name"] if cand_a else None, cand_b["table_name"] if cand_b else None] if x],
            "chart_hint": "bar",
        }

    metric_name = _friendly_metric_name(query, cand_a)
    alias_a = f"{_sql_alias_from_name(metric_name)}_{year_a}"
    alias_b = f"{_sql_alias_from_name(metric_name)}_{year_b}"
    state_filter = _extract_state_filter_code(query)
    where_a = _state_filter_where_clause(state_filter)
    where_b = _state_filter_where_clause(state_filter)

    agg_a = "MEDIAN" if _query_metric_family(query) in {"income", "rent"} else "SUM"
    agg_b = "MEDIAN" if _query_metric_family(query) in {"income", "rent"} else "SUM"

    extra = f', (b.{_quote_ident(alias_b)} - a.{_quote_ident(alias_a)}) AS "ABSOLUTE_CHANGE"'
    if _is_growth_query(query):
        extra += (
            f', CASE WHEN a.{_quote_ident(alias_a)} = 0 THEN NULL '
            f'ELSE ((b.{_quote_ident(alias_b)} - a.{_quote_ident(alias_a)}) / a.{_quote_ident(alias_a)}) * 100 END AS "PERCENT_CHANGE"'
        )

    sql = (
        f"SELECT a.{_quote_ident(alias_a)}, b.{_quote_ident(alias_b)}{extra} "
        f"FROM (SELECT {agg_a}({_quote_ident(cand_a['column_name'])}) AS {_quote_ident(alias_a)} FROM {_quote_ident(cand_a['table_name'])} {where_a}) a "
        f"CROSS JOIN (SELECT {agg_b}({_quote_ident(cand_b['column_name'])}) AS {_quote_ident(alias_b)} FROM {_quote_ident(cand_b['table_name'])} {where_b}) b"
    )

    def _builder(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return "The comparison query returned no rows."
        row = rows[0]
        av = row.get(alias_a) or row.get(alias_a.upper())
        bv = row.get(alias_b) or row.get(alias_b.upper())
        delta = row.get("ABSOLUTE_CHANGE")
        scope = f" in {STATE_FIPS_TO_NAME.get(state_filter, state_filter)}" if state_filter else ""

        if _is_growth_query(query):
            pct = row.get("PERCENT_CHANGE")
            return (
                f"The {metric_name}{scope} in {year_a} is {_format_value(av)}, "
                f"in {year_b} is {_format_value(bv)}, for an absolute change of {_format_value(delta)} "
                f"and a percent change of {_format_value(pct)}."
            )

        return (
            f"The {metric_name}{scope} in {year_a} is {_format_value(av)} and in {year_b} is {_format_value(bv)}, "
            f"for an absolute change of {_format_value(delta)}."
        )

    return _run_sql_answer(
        question=query,
        sql=sql,
        answer_builder=_builder,
        selected_tables=[cand_a["table_name"], cand_b["table_name"]],
        chart_hint="bar",
    )


def _answer_ranked(query: str) -> Optional[Dict[str, Any]]:
    years = _years_in_query(query)
    year = years[0] if years else None
    candidate = _resolve_metric_candidate(query, year=year)
    if candidate is None:
        return None

    metric_name = _friendly_metric_name(query, candidate)
    metric_alias = _sql_alias_from_name(metric_name)
    target = _group_target(query)

    if target == "state":
        group_expr, group_alias = f"SUBSTR({_cbg_expr()}, 1, 2)", "STATE_FIPS"
    elif target == "county":
        group_expr, group_alias = f"SUBSTR({_cbg_expr()}, 1, 5)", "COUNTY_FIPS"
    else:
        group_expr, group_alias = _quote_ident("CENSUS_BLOCK_GROUP"), "CENSUS_BLOCK_GROUP"

    state_filter = _extract_state_filter_code(query)
    where_clause = _state_filter_where_clause(state_filter)
    order_dir = "ASC" if "bottom" in query.lower() else "DESC"

    sql = (
        f"SELECT {group_expr} AS {_quote_ident(group_alias)}, "
        f"SUM({_quote_ident(candidate['column_name'])}) AS {_quote_ident(metric_alias)} "
        f"FROM {_quote_ident(candidate['table_name'])} {where_clause} "
        f"GROUP BY 1 ORDER BY {_quote_ident(metric_alias)} {order_dir} LIMIT {_rank_n(query)}"
    )

    def _builder(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return "The ranking query returned no rows."

        previews = []
        for row in rows[:5]:
            if group_alias == "STATE_FIPS":
                group_val = row.get("STATE_NAME") or row.get("STATE_FIPS")
            elif group_alias == "COUNTY_FIPS":
                group_val = row.get("COUNTY_NAME") or row.get("COUNTY_FIPS")
            else:
                group_val = row.get(group_alias)

            value = row.get(metric_alias) or row.get(metric_alias.upper())
            previews.append(f"{group_val}: {_format_value(value)}")

        prefix = _resolved_year_phrase(query, candidate.get("year"))
        return f"{prefix}I found {len(rows)} rows. First rows: " + " | ".join(previews)

    return _run_sql_answer(
        question=query,
        sql=sql,
        answer_builder=_builder,
        selected_tables=[candidate["table_name"]],
        chart_hint="bar",
        year=year,
        postprocess_geo=(group_alias in {"STATE_FIPS", "COUNTY_FIPS"}),
    )


def _answer_area_stats(query: str) -> Optional[Dict[str, Any]]:
    q = query.lower()
    if not any(term in q for term in ["land", "water", "latitude", "longitude"]):
        return None

    registry = get_table_registry(force_refresh=False)
    years = _years_in_query(query)
    year = years[0] if years else None

    geo_name = None
    for item in registry:
        if item["table_kind"] == "geography" and (year is None or item.get("year") == year):
            geo_name = item["table_name"]
            break

    if not geo_name:
        return None

    metric_col = None
    if "land" in q:
        metric_col = "AMOUNT_LAND"
    elif "water" in q:
        metric_col = "AMOUNT_WATER"
    elif "latitude" in q:
        metric_col = "LATITUDE"
    elif "longitude" in q:
        metric_col = "LONGITUDE"

    if not metric_col:
        return None

    sql = (
        f'SELECT AVG({_quote_ident(metric_col)}) AS "AVERAGE_VALUE", '
        f'MIN({_quote_ident(metric_col)}) AS "MIN_VALUE", '
        f'MAX({_quote_ident(metric_col)}) AS "MAX_VALUE" '
        f'FROM {_quote_ident(geo_name)}'
    )

    def _builder(rows: List[Dict[str, Any]]) -> str:
        row = rows[0]
        nice = metric_col.lower().replace("_", " ")
        prefix = _resolved_year_phrase(query, year)
        return (
            f"{prefix}for {nice}, the average is {_format_value(row.get('AVERAGE_VALUE'))}, "
            f"the minimum is {_format_value(row.get('MIN_VALUE'))}, and the maximum is {_format_value(row.get('MAX_VALUE'))}."
        )

    return _run_sql_answer(
        question=query,
        sql=sql,
        answer_builder=_builder,
        selected_tables=[geo_name],
        chart_hint="table",
    )


def _answer_deterministic(query: str) -> Optional[Dict[str, Any]]:
    if _off_topic(query):
        return {
            "status": "off_topic",
            "question": query,
            "answer": "That question does not seem to be about the census dataset.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [],
            "chart_hint": "table",
        }

    area = _answer_area_stats(query)
    if area:
        return area

    if _is_compare_query(query):
        result = _answer_compare_years(query)
        if result:
            return result

    if _is_ranking_query(query):
        result = _answer_ranked(query)
        if result:
            return result

    result = _answer_total_metric(query)
    if result:
        return result

    return None


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json|sql)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    cleaned = _strip_code_fences(text)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None

    return None


def _normalize_sql(sql: str) -> str:
    text = _strip_code_fences(sql).strip()
    if not text:
        return ""

    match = re.search(r"\b(select|with)\b", text, flags=re.IGNORECASE)
    if match:
        text = text[match.start():]

    if ";" in text:
        text = text.split(";", 1)[0]

    return text.strip()


def _is_read_only_sql(sql: str) -> bool:
    normalized = (sql or "").strip().lower()
    if not normalized or not re.match(r"^\s*(select|with)\b", normalized):
        return False

    banned = [" insert ", " update ", " delete ", " merge ", " drop ", " alter ", " create ", " truncate ", " grant ", " revoke ", " copy ", " call "]
    padded = f" {normalized} "
    return not any(tok in padded for tok in banned)


def _quote_known_table_names(sql: str, selected_tables: List[str]) -> str:
    updated = sql
    for table_name in sorted(selected_tables, key=len, reverse=True):
        updated = re.sub(rf'(?<!")\b{re.escape(table_name)}\b(?!")', f'"{table_name}"', updated)
    return updated


def _build_fallback_table_details(query: str) -> List[Dict[str, Any]]:
    years = _years_in_query(query)
    names: List[str] = []

    if years:
        for year in years:
            cand = _resolve_metric_candidate(query, year=year)
            if cand:
                names.append(cand["table_name"])
    else:
        cand = _resolve_metric_candidate(query)
        if cand:
            names.append(cand["table_name"])

    for name in _candidate_objects(query, year=years[0] if years else None, limit=4):
        if name not in names:
            names.append(name)

    return [get_table_details(name, force_refresh=False) for name in names[:4]]


def _schema_text(details: List[Dict[str, Any]], max_columns: int = 25) -> str:
    blocks: List[str] = []

    for detail in details:
        lines: List[str] = []
        for col in detail["columns"][:max_columns]:
            parts = [x for x in [col.get("label", ""), col.get("topic", ""), col.get("universe", "")] if x]
            if parts:
                lines.append(f'{col["name"]} ({col["type"]}) -- ' + " | ".join(parts))
            else:
                lines.append(f'{col["name"]} ({col["type"]})')

        header = f'Table: "{detail["table_name"]}" | Kind: {detail["table_kind"]}'
        if detail.get("year") is not None:
            header += f' | Year: {detail["year"]}'
        if detail.get("join_keys"):
            header += f' | Join keys: {", ".join(detail["join_keys"])}'

        blocks.append(header + "\nColumns:\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def _validate_sql_against_schema(sql: str, details: List[Dict[str, Any]]) -> Optional[str]:
    selected_tables = [d["table_name"] for d in details]
    table_columns = {
        d["table_name"]: {str(c.get("name") or "").upper() for c in d["columns"]}
        for d in details
    }

    alias_map: Dict[str, str] = {}
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+"?([A-Za-z0-9_]+)"?(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?',
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        table_name = match.group(1)
        alias = match.group(2) or table_name
        alias_map[alias] = table_name

    bad_tables = [t for t in alias_map.values() if t not in selected_tables]
    if bad_tables:
        return f"SQL referenced tables not in allowed schema: {sorted(set(bad_tables))}"

    bad_cols = []
    for alias, col in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b', sql):
        if alias not in alias_map:
            continue
        table_name = alias_map[alias]
        if col.upper() not in table_columns.get(table_name, set()):
            bad_cols.append(f"{alias}.{col} (table {table_name})")

    if bad_cols:
        return f"SQL referenced columns not present in selected schema: {bad_cols}"

    return None


def _llm_plan_sql(query: str, details: List[Dict[str, Any]]) -> Dict[str, Any]:
    schema = _schema_text(details)
    prompt = f"""
Return strict JSON with exactly these keys:
{{
  "can_answer": true,
  "reason": "short explanation",
  "sql": "SELECT ...",
  "chart_hint": "table"
}}

Rules:
- Use ONLY the tables and columns shown below.
- SQL must be read-only: SELECT or WITH only.
- Use quoted table names exactly as shown.
- Never include markdown fences.
- Never include explanatory text outside the JSON.
- Use actual join keys shown in the schema.
- If the question cannot be answered faithfully from the given schema, set can_answer=false.
- Prefer correctness over cleverness.
- chart_hint must be one of: table, bar, line, scatter.

SCHEMA:
{schema}

QUESTION:
{query}
""".strip()

    raw = call_llm(
        prompt=prompt,
        system_prompt="You are a precise production-grade Snowflake SQL planner. Return strict JSON only.",
        temperature=0.0,
        max_tokens=450,
    )

    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "can_answer": False,
            "reason": "Planner did not return valid JSON.",
            "sql": "",
            "chart_hint": _default_chart_hint(query),
        }

    parsed["sql"] = _quote_known_table_names(_normalize_sql(str(parsed.get("sql") or "")), [d["table_name"] for d in details])
    if not parsed.get("chart_hint"):
        parsed["chart_hint"] = _default_chart_hint(query)
    return parsed


def _llm_fix_sql(query: str, broken_sql: str, error_text: str, details: List[Dict[str, Any]]) -> str:
    schema = _schema_text(details)
    prompt = f"""
The following Snowflake SQL failed or violated schema validation.

QUESTION:
{query}

SCHEMA:
{schema}

BROKEN SQL:
{broken_sql}

ERROR:
{error_text}

Fix it.

Rules:
- Return only corrected read-only SQL.
- Use only the provided schema.
- Use quoted table names exactly as shown.
- Do not add markdown fences.
- Do not add explanatory text.
""".strip()

    raw = call_llm(
        prompt=prompt,
        system_prompt="You are a precise Snowflake SQL repair assistant. Return only corrected read-only SQL.",
        temperature=0.0,
        max_tokens=350,
    )
    return _quote_known_table_names(_normalize_sql(raw), [d["table_name"] for d in details])


def _summarize_rows(query: str, rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "The query ran successfully but returned no rows."

    rows = _lookup_geo_names(rows, year=_years_in_query(query)[0] if _years_in_query(query) else None)
    metric_name = _friendly_metric_name(query)

    if len(rows) == 1:
        non_null = [(k, v) for k, v in rows[0].items() if v is not None]
        if len(non_null) == 1:
            return f"The {metric_name} is {_format_value(non_null[0][1])}."
        return _single_row_summary(rows[0])

    previews = []
    for row in rows[:5]:
        parts = [f"{k}: {_format_value(v)}" for k, v in row.items() if v is not None]
        if parts:
            previews.append(", ".join(parts[:4]))

    return f"I found {len(rows)} rows. First rows: " + " | ".join(previews)


def _llm_fallback(query: str) -> Dict[str, Any]:
    details = _build_fallback_table_details(query)
    if not details:
        return {
            "status": "unanswerable",
            "question": query,
            "answer": "I could not map that question to a high-confidence census query.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [],
            "chart_hint": _default_chart_hint(query),
        }

    plan = _llm_plan_sql(query, details)
    if not bool(plan.get("can_answer")):
        return {
            "status": "unanswerable",
            "question": query,
            "answer": str(plan.get("reason") or "I cannot answer that faithfully from the selected tables."),
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [d["table_name"] for d in details],
            "chart_hint": str(plan.get("chart_hint") or _default_chart_hint(query)),
        }

    sql = str(plan.get("sql") or "")
    if not _is_read_only_sql(sql):
        return {
            "status": "error",
            "question": query,
            "answer": "The SQL planner did not generate valid read-only SQL.",
            "sql": sql or None,
            "result": [],
            "row_count": 0,
            "error": "Generated SQL was not a valid SELECT/WITH statement.",
            "selected_tables": [d["table_name"] for d in details],
            "chart_hint": str(plan.get("chart_hint") or _default_chart_hint(query)),
        }

    validation = _validate_sql_against_schema(sql, details)
    if validation:
        sql = _llm_fix_sql(query, sql, validation, details)

    try:
        rows = run_query(sql)
    except Exception as exc:
        sql = _llm_fix_sql(query, sql, str(exc), details)
        rows = run_query(sql)

    rows = _lookup_geo_names(rows, year=_years_in_query(query)[0] if _years_in_query(query) else None)

    return {
        "status": "ok",
        "question": query,
        "answer": _summarize_rows(query, rows),
        "sql": sql,
        "result": rows,
        "row_count": len(rows),
        "error": None,
        "selected_tables": [d["table_name"] for d in details],
        "chart_hint": str(plan.get("chart_hint") or _default_chart_hint(query)),
    }


def ask_question(user_query: str) -> Dict[str, Any]:
    unavailable_years = _unavailable_years_in_query(user_query)
    if unavailable_years:
        years_text = ", ".join(str(y) for y in unavailable_years)
        return {
            "status": "unanswerable",
            "question": user_query,
            "answer": f"Only 2019 and 2020 data are available in this dataset. The requested year(s) {years_text} are not available.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [],
            "chart_hint": _default_chart_hint(user_query),
        }

    lower_q = user_query.lower().strip()
    if re.fullmatch(r"(and\s+)?(for|in|of)\s+20\d{2}\??", lower_q):
        return {
            "status": "unanswerable",
            "question": user_query,
            "answer": "Question is incomplete or ambiguous.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [],
            "chart_hint": "table",
        }

    try:
        deterministic = _answer_deterministic(user_query)
        if deterministic is not None:
            return deterministic

        if ENABLE_LLM_FALLBACK:
            return _llm_fallback(user_query)

        return {
            "status": "unanswerable",
            "question": user_query,
            "answer": "I could not map that question to a high-confidence census query.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": None,
            "selected_tables": [],
            "chart_hint": _default_chart_hint(user_query),
        }

    except LLMRateLimitError as exc:
        return {
            "status": "error",
            "question": user_query,
            "answer": "The Groq model hit a rate limit. Please try again in a moment.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": str(exc),
            "selected_tables": [],
            "chart_hint": "table",
        }
    except LLMServiceError as exc:
        return {
            "status": "error",
            "question": user_query,
            "answer": "The Groq model is currently unavailable.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": str(exc),
            "selected_tables": [],
            "chart_hint": "table",
        }
    except Exception as exc:
        return {
            "status": "error",
            "question": user_query,
            "answer": "The system encountered an internal error while processing your question.",
            "sql": None,
            "result": [],
            "row_count": 0,
            "error": str(exc),
            "selected_tables": [],
            "chart_hint": "table",
        }