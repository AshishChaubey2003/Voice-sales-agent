# Evaluation Guide

> Voice Sales Agent — How to Run and Interpret Evaluations

## Overview

The evaluation system measures three aspects of the Voice Sales Agent:

1. **Retrieval quality** — Does the RAG system find relevant knowledge chunks?
2. **Response quality** — Are the LLM's answers grounded, accurate, and helpful?
3. **Consent/Lead gate** — Does the consent logic correctly save or reject leads?

## Datasets

Located in `evals/datasets/`:

- **Golden dataset** (~75 cases): Human-curated question/answer pairs with expected knowledge chunks
- **Candidate dataset** (~60 cases): Additional test cases for regression testing

## Running Evaluations

### Retrieval Evaluation (no LLM needed)

```bash
python -m evals.run --mode retrieval
```

Measures: recall@k, precision@k, MRR against the golden dataset.

### Full Evaluation (requires Groq API key)

```bash
# Requires explicit consent flag
python -m evals.run --mode full --i-accept-llm-costs
```

### Live Evaluation Dashboard

The dashboard at `/dashboard/evals` shows:
- Turn-level quality scores (last 30 days)
- Latency percentiles (p50, p95, p99)
- RAG retrieval metrics
- Consent gate pass/fail rates

Access: `http://localhost:8000/dashboard/evals`

### API Endpoints

```bash
# Latest live evaluation summary
curl -H "Authorization: Bearer $API_KEY" http://localhost:8000/v1/evals/live/summary

# Latency percentiles
curl -H "Authorization: Bearer $API_KEY" http://localhost:8000/v1/evals/live/latency

# Recent turns
curl -H "Authorization: Bearer $API_KEY" http://localhost:8000/v1/evals/live/turns
```

## Interpreting Results

| Metric | Good | Acceptable | Poor |
|---|---|---|---|
| Retrieval recall@4 | > 0.8 | 0.6–0.8 | < 0.6 |
| Response grounding | > 0.9 | 0.7–0.9 | < 0.7 |
| Consent accuracy | 1.0 | 0.95–1.0 | < 0.95 |
| LLM latency p50 | < 1s | 1–3s | > 3s |
| RAG latency p50 | < 200ms | 200–500ms | > 500ms |

## Human Review Workflow

See `docs/EVAL_REVIEWER_QUICKSTART.md` for the reviewer assignment and scoring workflow.
See `docs/EVAL_REVIEW_GUIDE.md` for detailed scoring criteria.
See `docs/EVAL_POLICY_DECISIONS.md` for policy decisions about evaluation edge cases.

## Important Notes

- **Never fabricate evaluation results.** Only report metrics from actual evaluation runs.
- Human-reviewed metrics require actual human review — the system tracks which cases have been reviewed.
- The golden dataset is reference data, not a test fixture — do not modify it to pass tests.
