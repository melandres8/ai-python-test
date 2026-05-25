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
