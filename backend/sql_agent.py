years = _years_in_query(query)
if not years:
    return {
        "status": "unanswerable",
        "question": query,
        "answer": f"I could not extract any years from the query.",
        "sql": None,
        "result": [],
        "row_count": 0,
        "error": None,
        "selected_tables": [],
        "chart_hint": "bar",
    }
if len(years) < 2:
    return {
        "status": "unanswerable",
        "question": query,
        "answer": f"I could not extract at least two years from the query.",
        "sql": None,
        "result": [],
        "row_count": 0,
        "error": None,
        "selected_tables": [],
        "chart_hint": "bar",
    }