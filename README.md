# 📊 US Census AI Agent

An AI-powered agent that converts natural language queries into optimized SQL queries over Snowflake Census datasets, delivering fast, accurate, and context-aware insights.

---

## 🚀 Overview

This project enables users to query US Census data using natural language. The system understands user intent, generates SQL queries, executes them on Snowflake, and returns accurate results within seconds.

---

## ✨ Features

- Natural Language → SQL conversion (AI Agent)
- Fast response time (< 3 seconds)
- Follow-up query handling (context-aware)
- Supports comparisons (2019 vs 2020)
- State and county-level aggregation
- Strict hallucination prevention
- Read-only SQL enforcement for safety
- Deterministic + LLM hybrid architecture

---

## 🏗️ Architecture

User Query → FastAPI Backend → SQL Agent → Snowflake → Response

---

## 🧠 How It Works

1. Query Understanding  
   Extracts intent (population, income, rent, etc.), detects year, and identifies grouping.

2. SQL Generation  
   Selects correct tables and columns using schema metadata and generates optimized SQL.

3. Execution  
   Executes SQL on Snowflake.

4. Post-processing  
   Converts FIPS codes into readable state and county names.

---

## 🛠️ Tech Stack

- Backend: FastAPI  
- Database: Snowflake  
- Language: Python  
- LLM (optional fallback): Groq  
- Frontend: Streamlit  

---

## 📂 Project Structure

backend/
- main.py  
- sql_agent.py  
- db.py  
- schema_service.py  
- llm.py  

frontend/
- app.py  

---

## 🔑 Environment Setup

Create a `.env` file in the root directory:

```env
SNOWFLAKE_USER=your_username
SNOWFLAKE_PASSWORD=your_password
SNOWFLAKE_ACCOUNT=your_account
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_DATABASE=US_OPEN_CENSUS_DATA__NEIGHBORHOOD_INSIGHTS__FREE_DATASET
SNOWFLAKE_SCHEMA=PUBLIC
SNOWFLAKE_ROLE=ACCOUNTADMIN

# Optional (for LLM fallback)
GROQ_API_KEY=your_groq_api_key



⚙️ Installation
git clone <your-repo-url>
cd project
pip install -r requirements.txt
▶️ Running the Project

Start backend:

uvicorn backend.main:app --reload --port 8000

Start frontend:

streamlit run frontend/app.py
🧪 Example Queries
What is the total population?
What is the female population?
Top 5 states by population
Top 5 counties by population
Compare population between 2019 and 2020
What is the population in California?
Same for Texas
And in 2020?
🚫 Out-of-Scope Handling

The system correctly rejects unrelated queries.

Example:

GDP of USA
⚡ Performance
Average response time: 1–3 seconds
No hallucinated answers
Optimized SQL queries
🧩 Challenges & Solutions
Mapping natural language to schema → solved using metadata-driven selection
Handling follow-ups → solved using context-aware query rewriting
Avoiding hallucinations → solved using deterministic pipeline
Performance optimization → minimized LLM usage
📈 Future Improvements
Add more census years
Expand supported metrics
Improve NLP understanding
Add advanced visualizations
