# AI Intelligent Notification Service Architecture

## Executive summary

This solution implements the required FastAPI service on port 5000 for natural-language notification intents. It accepts user text, asks the mock AI provider for structured extraction, applies guardrails for noisy LLM output, falls back to deterministic extraction when the AI refuses or returns unusable data, and sends valid notifications to the provider.

## Component map

```mermaid
flowchart LR
    Client[Client / k6] -->|POST /v1/requests user_input| API[FastAPI app/main.py]
    Client -->|POST /v1/requests/{id}/process| API
    Client -->|GET /v1/requests/{id}| API
    API --> Store[(In-memory request store)]
    API --> Worker[Background processing task]
    Worker --> Prompt[System prompt + AI request]
    Prompt -->|X-API-Key| AIProvider[AI Extract :3001]
    AIProvider --> Guardrails[Markdown stripping + relaxed JSON parser + aliases]
    Guardrails --> HeuristicFallback[Regex fallback for email/phone/message]
    HeuristicFallback --> NotifyRetry[Notify retry + concurrency guard]
    NotifyRetry -->|X-API-Key| NotificationProvider[Notify :3001]
```

## Runtime/data flow

1. `POST /v1/requests` validates `{user_input}` and returns `201 {id}`.
2. `POST /v1/requests/{id}/process` marks the item as `processing` and schedules background extraction/delivery.
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
