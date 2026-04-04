from backend.sql_agent import ask_question

def test_population():
    res = ask_question("What is the total population?")
    assert res["status"] == "ok"

def test_off_topic():
    res = ask_question("GDP of USA")
    assert res["status"] == "off_topic"

def test_compare():
    res = ask_question("Compare population between 2019 and 2020")
    assert res["status"] == "ok"