from sql_agent import ask_question

query = "What is the total population?"

response = ask_question(query)

print("\nQUESTION:")
print(response["question"])

print("\nGENERATED SQL:")
print(response["sql"])

print("\nANSWER:")
print(response["answer"])