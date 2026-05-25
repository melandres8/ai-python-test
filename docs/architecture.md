# AI Intelligent Notification Service Architecture

## Executive summary

This solution implements the required FastAPI service on port 5000 for natural-language notification intents. It accepts user text, asks the mock AI provider for structured extraction, applies guardrails for noisy LLM output, falls back to deterministic extraction when the AI refuses or returns unusable data, and sends valid notifications to the provider.

## Component map

```mermaid
flowchart LR
    Client[Client / k6] -->|POST /v1/requests user_input| API[FastAPI routes app/main.py]
    Client -->|POST /v1/requests/{id}/process| API
    Client -->|GET /v1/requests/{id}| API
    API --> Models[models.py request/status schemas]
    API --> Store[(store.py in-memory request store)]
    API --> Worker[Background processing task]
    Worker --> Extractor[ai_extractor.py prompt + guardrails + fallback]
    Extractor -->|X-API-Key| AIProvider[AI Extract :3001]
    Extractor --> ProviderClient[provider_client.py notify retry + concurrency guard]
    ProviderClient -->|X-API-Key| NotificationProvider[Notify :3001]
```

## Runtime/data flow

1. `POST /v1/requests` validates `{user_input}` and returns `201 {id}`.
2. `POST /v1/requests/{id}/process` marks a queued item as `processing` and schedules background extraction/delivery. The operation is idempotent: repeated calls while already `processing`, `sent`, or `failed` return the current state without enqueueing duplicate work.
3. The worker calls `/v1/ai/extract` using a strict system prompt that asks for compact JSON with `to`, `message`, and `type`.
4. Guardrails parse common LLM response variants:
   - Markdown fenced JSON.
   - Embedded JSON inside prose.
   - Capitalized or aliased keys (`Recipient/body/channel`, `destination/text/method`).
   - Single quotes or unquoted keys.
   - Truncated JSON with trailing ellipsis where recoverable.
5. If AI output is not usable, the service uses deterministic extraction from the original prompt for email/phone and message text.
6. Valid notifications are sent to `/v1/notify` with bounded timeout, concurrency limits, trace id, and retries for transient failures.
7. `GET /v1/requests/{id}` returns `queued`, `processing`, `sent`, or `failed`.

## Operational notes

- Storage is in-memory because the challenge defines no persistent database.
- Provider URL/API key/timeouts/concurrency are environment-configurable.
- The status endpoint remains valid during the AI latency window; long extractions report `processing` instead of blocking client requests.

## Production trade-offs

- **Persistence:** in-memory state is adequate for the challenge evaluator, but production should persist request state and extracted notification payloads in Postgres/Redis for restart safety and auditability.
- **Durable processing:** FastAPI `BackgroundTasks` keeps the implementation small. A production system should use a durable queue/worker to survive crashes, support dead-lettering, and isolate slow AI/provider calls from API workers.
- **Idempotency:** `/process` is idempotent at the application state level. Production should enforce the queued-to-processing transition atomically in the datastore and consider idempotency keys for client retries.
- **AI extraction:** guardrails handle common mock-LLM noise. Production should prefer schema-constrained model output where possible, retain parser tests for adversarial examples, and log structured extraction-failure reasons.
- **Secrets:** the README-provided API key is used as a default for the challenge. Production secrets should be environment-only and managed by the deployment platform.
- **Observability:** production should add structured logs, metrics, tracing, and dashboards for AI latency, extraction failures, notification retries, and provider error rates.

## Verification

```bash
cd app
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q test_main.py
python -m compileall -q main.py test_main.py
```

A local smoke check can be run by starting `provider/app.py` on port 3001 and `app/main.py` on port 5000, creating an intent, processing it, and polling status until `sent` or `failed`.
