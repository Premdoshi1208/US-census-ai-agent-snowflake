from db import run_query

result = run_query("SHOW TABLES;")

for r in result[:10]:
    print(r["name"])