import os
import time
from typing import Optional

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Prevent very expensive defaults from the environment from slowing common paths.
_env_primary = (os.getenv("GROQ_MODEL") or "").strip()
_env_fallback = (os.getenv("GROQ_FALLBACK_MODEL") or "").strip()

if "120b" in _env_primary.lower():
    _env_primary = "openai/gpt-oss-20b"

PRIMARY_MODEL = _env_primary or "openai/gpt-oss-20b"
FALLBACK_MODEL = _env_fallback or ""

MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "1"))
RETRY_SLEEP_SECONDS = float(os.getenv("GROQ_RETRY_SLEEP_SECONDS", "0.3"))


class LLMServiceError(Exception):
    pass


class LLMRateLimitError(LLMServiceError):
    pass


def _error_text(exc: Exception) -> str:
    try:
        return str(exc)
    except Exception:
        return "Unknown LLM error."


def _single_call(
    *,
    prompt: str,
    system_prompt: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )

    content = response.choices[0].message.content
    if not content:
        raise LLMServiceError(f"Model {model} returned an empty response.")
    return content.strip()


def call_llm(
    prompt: str,
    system_prompt: str = "You are a precise and helpful data analyst.",
    temperature: float = 0.0,
    max_tokens: int = 400,
) -> str:
    models = [m for m in [PRIMARY_MODEL, FALLBACK_MODEL] if m]
    errors = []

    for model in models:
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                return _single_call(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                last_error = exc
                text = _error_text(exc).lower()

                if "rate limit" in text or "429" in text:
                    break

                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_SLEEP_SECONDS)

        errors.append(f"{model}: {_error_text(last_error) if last_error else 'Unknown error'}")

    combined = " | ".join(errors)

    if "rate limit" in combined.lower() or "429" in combined.lower():
        raise LLMRateLimitError(f"LLM rate limit reached. Details: {combined}")

    raise LLMServiceError(f"All configured models failed. Details: {combined}")