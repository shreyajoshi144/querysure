# DeepEval integration

DeepEval is an **additional** evaluation layer for QuerySure. It does not change
`retrieve_context -> generate_sql -> validate_sql -> execute_query`, and it does
not replace the existing 62-case deterministic evaluator.

## Source-of-truth rules

The shipped `evaluation/datasets/retail_benchmark.json` remains exactly 62 cases.
Its SHA-256 is:

`e2150163a71b2448754129e428685f7dcd11015cb6c1a5fc21017ddabfe4b4a0`

The existing evaluator remains authoritative for:

- SQL validity
- execution success
- result-set correctness/equivalence
- hallucinated identifiers
- schema/table/column grounding
- rejection/clarification accuracy

DeepEval scores are supplementary LLM-as-a-judge signals. They never override a
deterministic pass/fail result.

## 1. Deterministic/local evaluation — no API key

These are the existing evaluation commands and remain the cheapest regression
gates:

```bash
python -m evaluation validate-dataset
python -m evaluation run --mode oracle
python -m evaluation run --mode synthetic
python -m evaluation run --mode replay --responses evaluation/results/run1_responses.json
```

`oracle` is a harness self-check; it is not an LLM-quality measurement.

## 2. DeepEval on the 62 golden cases

DeepEval needs a judge model. The adapter uses QuerySure's existing provider
configuration in the same order as the application: Groq -> OpenAI -> Anthropic.
No separate judge API key is required.

For a real QuerySure + DeepEval run:

```bash
export GROQ_API_KEY=...
python -m evaluation deepeval-golden --mode live --yes
```

or use OpenAI/Anthropic if those are the configured providers:

```bash
export OPENAI_API_KEY=...
python -m evaluation deepeval-golden --mode live --yes --judge-provider openai
```

Use a recorded QuerySure response file to avoid calling the application LLM again:

```bash
python -m evaluation deepeval-golden \
  --mode replay \
  --responses evaluation/results/run1_responses.json \
  --judge-provider openai
```

The replay removes application-generation cost, but DeepEval still calls its judge
model. Use `--judge-provider` / `--judge-model` or the environment variables:

```bash
DEEPEVAL_JUDGE_PROVIDER=openai
DEEPEVAL_JUDGE_MODEL=gpt-4.1
```

The report contains both the unchanged QuerySure deterministic report and the
supplementary DeepEval metrics.

## 3. Synthetic expansion

Synthetic generation is intentionally separate from the 62-case benchmark:

```bash
python -m evaluation deepeval-synthetic --num 25
```

DeepEval's Golden Synthesizer generates additional natural-language questions.
These questions are **not automatically promoted into the deterministic golden
benchmark**, because synthetic generation does not provide a trusted SQL/result
ground truth. They can be run through QuerySure for supplementary coverage:

```bash
python -m evaluation deepeval-synthetic --num 25 --run
```

The generated dataset and run report are written under `evaluation/results/`.
Inspect synthetic cases before adding any to the deterministic benchmark.

## DeepEval metrics used

The adapter keeps the metric set small:

- `AnswerRelevancyMetric` — whether the generated QuerySure output addresses the
  natural-language question.
- `FaithfulnessMetric` — whether the generated output is grounded in the trusted
  schema context supplied to the evaluator.

These metrics are useful for qualitative/LLM-level signals, but they do **not**
replace result equivalence or identifier checks.

## Cost and safety

- `oracle`, `synthetic`, and deterministic `replay` remain network-free.
- `deepeval-golden --mode live` calls QuerySure's real LLM provider chain and also
  calls a DeepEval judge, so it can spend money twice per case.
- Synthetic generation calls the DeepEval judge/generator and therefore requires
  a configured provider.
- No accuracy number is hardcoded or inferred from synthetic generation.
