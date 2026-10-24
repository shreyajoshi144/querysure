"""
Where the LLM's text comes from.

QuerySure's real pipeline is: retrieve_context -> generate_sql -> validate_sql
-> execute_query. generate_sql() builds the prompt, calls an LLM provider
chain, then parses SQL / confidence out of the reply. The evaluation runs
that *entire real function*; the only thing that differs between modes is
what the provider chain returns:

  scripted (oracle / synthetic / replay)
      The single transport function `_run_provider_chain` is temporarily
      swapped, inside a context manager, for one that returns pre-written
      text in the exact format generate_sql's prompt asks the LLM for.
      Prompt building, SQL extraction, confidence parsing and the
      retrieval-confidence downgrade all still run for real. Nothing on disk
      is modified and the original function is restored on exit.

  live
      No swap. Real providers are called with real API keys and cost real
      money. This is the only mode that says anything about LLM quality.

Sources never look at gold SQL except the oracle, which exists precisely to
prove the harness + dataset are self-consistent.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from .models import BenchmarkCase

# A refusal is now a structured reply with an empty ``sql`` (Sprint 3 JSON contract) whose
# explanation is this text.
REFUSAL_TEXT = (
    "I can't answer that from the connected database. The request cannot be "
    "expressed as a read-only query over the available tables and columns, so "
    "no query was produced. Please rephrase it or specify what you need."
)


def format_llm_response(
    sql: str,
    confidence: str = "High",
    complexity: str = "Moderate",
    explanation: str = "Generated for evaluation.",
) -> str:
    """Text in the exact format generate_sql()'s prompt requests (a single JSON object)."""
    import json
    return json.dumps({"sql": sql, "explanation": explanation, "confidence": confidence, "complexity": complexity})


def format_refusal(text: str = REFUSAL_TEXT) -> str:
    import json
    return json.dumps({"sql": "", "explanation": text, "confidence": "Low", "complexity": "Simple"})


@dataclass
class ScriptedResponse:
    raw_text: Optional[str] = None
    provider_error: Optional[str] = None

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> "ScriptedResponse":
        if entry.get("provider_error"):
            return cls(provider_error=str(entry["provider_error"]))
        if "raw_response" in entry:
            return cls(raw_text=str(entry["raw_response"]))
        if "refusal" in entry:
            return cls(raw_text=format_refusal(str(entry["refusal"])))
        if "sql" in entry:
            return cls(raw_text=format_llm_response(
                entry["sql"],
                confidence=entry.get("confidence", "High"),
                complexity=entry.get("complexity", "Moderate"),
                explanation=entry.get("explanation", "Generated for evaluation."),
            ))
        raise ValueError(f"scripted entry needs one of sql/refusal/raw_response/provider_error: {dict(entry)}")


class ResponseSource:
    """Interface the runner uses. Subclasses: ScriptedSource, LiveSource."""

    name: str = "source"
    calls_llm: bool = False
    description: str = ""

    @contextmanager
    def activate(self) -> Iterator["ResponseSource"]:
        yield self

    def begin_case(self, case: BenchmarkCase) -> None:  # noqa: D401
        """Called before each case."""


class ScriptedSource(ResponseSource):
    calls_llm = False

    def __init__(self, name: str, responses: Mapping[str, ScriptedResponse], description: str = ""):
        self.name = name
        self.description = description
        self._responses = dict(responses)
        self._current: Optional[str] = None

    def begin_case(self, case: BenchmarkCase) -> None:
        self._current = case.id

    def response_for(self, case_id: str) -> Optional[ScriptedResponse]:
        return self._responses.get(case_id)

    @contextmanager
    def activate(self) -> Iterator["ScriptedSource"]:
        from modules import sql_generator  # imported lazily: heavy + needs config

        original = sql_generator._run_provider_chain
        source = self

        def scripted_chain(system_prompt: str, user_prompt: str, groq_api_key: str):
            entry = source._responses.get(source._current or "")
            if entry is None:
                raise sql_generator.ProviderError(
                    f"scripted source '{source.name}' has no response for case '{source._current}'"
                )
            if entry.provider_error:
                raise sql_generator.ProviderError(entry.provider_error)
            label = f"scripted:{source.name}"
            return entry.raw_text or "", label, [sql_generator.ProviderAttempt(name=label)]

        sql_generator._run_provider_chain = scripted_chain
        try:
            yield self
        finally:
            sql_generator._run_provider_chain = original


class CredentialsMissing(RuntimeError):
    pass


class LiveSource(ResponseSource):
    """Real LLM providers via QuerySure's own provider chain. REQUIRES credentials
    (GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY in the environment, or
    --api-key for Groq) and spends real money."""

    name = "live"
    calls_llm = True
    description = "Real LLM providers (requires API keys; costs money)."

    def __init__(self, api_key: str = "", delay_seconds: float = 0.0):
        self.api_key = (api_key or "").strip()
        self.delay_seconds = max(0.0, float(delay_seconds))
        self._first = True

    def _configured_keys(self) -> Dict[str, bool]:
        import config
        return {
            "groq": bool(self.api_key or (config.GROQ_API_KEY or "").strip()),
            "openai": bool((config.OPENAI_API_KEY or "").strip()),
            "anthropic": bool((config.ANTHROPIC_API_KEY or "").strip()),
        }

    def effective_api_key(self) -> str:
        import config
        return self.api_key or (config.GROQ_API_KEY or "").strip()

    def check_credentials(self) -> None:
        if not any(self._configured_keys().values()):
            raise CredentialsMissing(
                "No LLM credentials found. Set GROQ_API_KEY, OPENAI_API_KEY or "
                "ANTHROPIC_API_KEY in the environment (or pass --api-key for Groq). "
                "Offline evaluation does not need them: `python -m evaluation run --mode oracle`."
            )

    def provider_chain_names(self) -> List[str]:
        """Names of the providers that would be tried, in order (no network calls)."""
        from modules import sql_generator
        return [p["name"] for p in sql_generator._build_provider_chain(self.effective_api_key())]

    def begin_case(self, case: BenchmarkCase) -> None:
        if not self._first and self.delay_seconds:
            time.sleep(self.delay_seconds)
        self._first = False


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_oracle_source(cases: Sequence[BenchmarkCase]) -> ScriptedSource:
    """Answers every answerable case with its gold SQL and refuses the rest.

    This is a SELF-CHECK of harness + dataset (gold SQL must validate, execute
    and compare equal to itself; refusals must count as correct rejections).
    Its perfect score says nothing about QuerySure's quality.
    """
    responses: Dict[str, ScriptedResponse] = {}
    for c in cases:
        if c.should_decline:
            responses[c.id] = ScriptedResponse(raw_text=format_refusal())
        else:
            responses[c.id] = ScriptedResponse(raw_text=format_llm_response(c.gold_sql or "", "High", "Moderate"))
    return ScriptedSource(
        "oracle", responses,
        description="Gold SQL for answerable cases, refusal for the rest (harness self-check).",
    )


def load_responses_file(path: str) -> Dict[str, ScriptedResponse]:
    """Load {case_id: entry} (optionally nested under a 'responses' key)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("responses", data) if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: expected an object mapping case ids to responses")
    return {cid: ScriptedResponse.from_entry(e) for cid, e in entries.items() if isinstance(e, dict)}


def build_replay_source(path: str) -> ScriptedSource:
    return ScriptedSource(
        "replay", load_responses_file(path),
        description=f"Recorded responses replayed from {path} (no LLM calls).",
    )


def load_synthetic_spec(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_synthetic_source(cases: Sequence[BenchmarkCase], spec: Mapping[str, Any]) -> ScriptedSource:
    """Oracle baseline + hand-authored defects from the fixture file.

    The defects are SYNTHETIC: they imitate typical LLM failure modes so the
    metrics can be exercised end to end. They are not measurements of a real
    model.
    """
    base = build_oracle_source(cases)
    responses = dict(base._responses)
    for cid, entry in (spec.get("overrides") or {}).items():
        if cid in responses:  # ignore overrides for cases filtered out of this run
            responses[cid] = ScriptedResponse.from_entry(entry)
    return ScriptedSource(
        "synthetic", responses,
        description="Oracle baseline with injected, labelled defects (synthetic demo, not a real model).",
    )
