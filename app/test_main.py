import asyncio
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

import main


def test_ai_lifecycle_extracts_noisy_response_and_notifies(monkeypatch):
    notified = []

    async def fake_extract(user_input):
        return 'He extraído: ```json\n{"to":"ana@example.com","message":"hola","type":"email","extra":true}\n```'

    async def fake_notify(notification, request_id):
        notified.append(notification.model_dump())
        return True

    monkeypatch.setattr(main, "extract_with_ai", fake_extract)
    monkeypatch.setattr(main, "send_notification_with_retries", fake_notify)

    with TestClient(main.app) as client:
        created = client.post("/v1/requests", json={"user_input": "Manda un mail a ana@example.com diciendo hola"})
        assert created.status_code == 201
        request_id = created.json()["id"]

        processed = client.post(f"/v1/requests/{request_id}/process")
        assert processed.status_code in (200, 202)
        assert client.get(f"/v1/requests/{request_id}").json() == {"id": request_id, "status": "sent"}
        assert notified == [{"to": "ana@example.com", "message": "hola", "type": "email"}]


def test_ai_lifecycle_falls_back_to_input_when_ai_refuses(monkeypatch):
    notified = []

    async def fake_extract(user_input):
        return "Lo siento, no puedo procesar datos personales."

    async def fake_notify(notification, request_id):
        notified.append(notification.model_dump())
        return True

    monkeypatch.setattr(main, "extract_with_ai", fake_extract)
    monkeypatch.setattr(main, "send_notification_with_retries", fake_notify)

    with TestClient(main.app) as client:
        request_id = client.post("/v1/requests", json={"user_input": "SMS al 600-111-222: cita confirmada"}).json()["id"]
        response = client.post(f"/v1/requests/{request_id}/process")
        assert response.status_code in (200, 202)
        assert client.get(f"/v1/requests/{request_id}").json()["status"] == "sent"
        assert notified[0]["to"] == "600-111-222"
        assert notified[0]["type"] == "sms"


def test_malformed_or_missing_destination_fails_cleanly(monkeypatch):
    async def fake_extract(user_input):
        return "{}"

    monkeypatch.setattr(main, "extract_with_ai", fake_extract)

    with TestClient(main.app) as client:
        request_id = client.post("/v1/requests", json={"user_input": "manda algo sin destino"}).json()["id"]
        client.post(f"/v1/requests/{request_id}/process")
        assert client.get(f"/v1/requests/{request_id}").json()["status"] == "failed"


def test_process_is_idempotent_while_request_is_processing():
    async def scenario():
        async with main._store_lock:
            main.requests_store.clear()
            request_id = "idempotent-ai-request"
            main.requests_store[request_id] = main.StoredRequest(
                id=request_id,
                user_input="Manda un mail a ana@example.com diciendo hola",
            )

        first_tasks = main.BackgroundTasks()
        first_response = await main.process_request(request_id, first_tasks)

        second_tasks = main.BackgroundTasks()
        second_response = await main.process_request(request_id, second_tasks)

        assert first_response == {"id": request_id, "status": main.RequestStatus.processing}
        assert second_response == {"id": request_id, "status": main.RequestStatus.processing}
        assert len(first_tasks.tasks) == 1
        assert len(second_tasks.tasks) == 0

    asyncio.run(scenario())


def test_ai_parser_handles_common_noisy_variants():
    variants = [
        "```json\n{to:'ana@example.com', message:'hola', type:'mail'}\n```",
        'Claro: {"recipient":"ana@example.com","body":"hola","channel":"email"}',
        '{destino:"600111222", mensaje:"hola sms", tipo:"telefono"}',
    ]

    parsed = [main.parse_ai_notification(content) for content in variants]

    assert parsed[0] == main.NotificationRequest(to="ana@example.com", message="hola", type=main.NotificationType.email)
    assert parsed[1] == main.NotificationRequest(to="ana@example.com", message="hola", type=main.NotificationType.email)
    assert parsed[2] == main.NotificationRequest(to="600111222", message="hola sms", type=main.NotificationType.sms)


def test_notification_provider_retries_transient_failure_then_succeeds(monkeypatch):
    statuses = [503, 200]
    observed_payloads = []

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, params, json):
            observed_payloads.append({"url": url, "headers": headers, "params": params, "json": json})
            status_code = statuses.pop(0)
            return SimpleNamespace(
                status_code=status_code,
                text="temporary failure" if status_code >= 500 else "ok",
                json=lambda: {"provider_id": "provider-123"},
            )

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    main.requests_store["retry-request"] = main.StoredRequest(
        id="retry-request",
        user_input="Manda un mail a ana@example.com diciendo hola",
    )

    delivered = asyncio.run(
        main.send_notification_with_retries(
            main.NotificationRequest(to="ana@example.com", message="hola", type=main.NotificationType.email),
            "retry-request",
        )
    )

    assert delivered is True
    assert len(observed_payloads) == 2
    assert observed_payloads[0]["headers"]["X-API-Key"] == "test-dev-2026"
    assert main.requests_store["retry-request"].provider_id == "provider-123"


def test_notification_provider_returns_false_after_timeout(monkeypatch):
    attempts = 0

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, params, json):
            nonlocal attempts
            attempts += 1
            raise httpx.TimeoutException("provider timed out")

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    delivered = asyncio.run(
        main.send_notification_with_retries(
            main.NotificationRequest(to="ana@example.com", message="hola", type=main.NotificationType.email),
            "timeout-request",
        )
    )

    assert delivered is False
    assert attempts == 4
