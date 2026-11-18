"""QuerySure AI provider layer (Sprint 2).

  ai.types      LLMResult, ProviderAttempt, ProviderError
  ai.providers  transports for Groq, OpenAI, Anthropic, Gemini and xAI
  ai.router     per-task provider/model routing (AI_TASK_ROUTES, LLM_PROVIDER_ORDER)
  ai.chain      fallback chain: circuit breakers, bounded retries, request deadline, attempt budget
  ai.usage      token usage and estimated cost
  ai.trace      per-attempt trace rows (ai_calls) grouped by trace id

Callers such as modules/sql_generator.py own their prompts and output contract; this package owns how a prompt reaches a
provider and how each attempt is accounted for. Agent code must call a task's owner (e.g. the SQL pipeline), never a
provider directly for SQL.
"""
