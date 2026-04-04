# Reflection

## Development Process
I started by building a deterministic SQL agent that maps natural language queries to schema metadata. Initially, I experimented with LLM-based SQL generation but shifted to a hybrid approach to ensure reliability and avoid hallucination.

## Key Architectural Decisions
- Used deterministic routing for common queries to ensure speed and correctness
- Added LLM fallback for flexibility
- Implemented strict SQL validation to prevent unsafe queries
- Designed schema-aware metric resolution instead of hardcoding

## Tradeoffs
- Focused on accuracy over full NLP generalization
- Limited dataset support to 2019–2020 for reliability
- Did not fully optimize UI/UX due to time constraints

## What I Would Improve
- Add support for more census years
- Improve natural language understanding
- Add caching for faster responses
- Improve visualization layer

## Edge Cases
- Ambiguous queries (e.g., "population growth")
- Unsupported years
- Partial follow-up queries
- Non-census queries

## Testing Approach
- Implemented basic unit tests for:
  - Valid queries
  - Comparisons
  - Off-topic detection
- With more time, I would add:
  - Integration tests
  - Load testing
  - Query fuzz testing