from __future__ import annotations

import asyncio
import ast
import logging
import os
import re
from typing import Any

import httpx
from pydantic import ValidationError

from models import NotificationRequest

logger = logging.getLogger("intelligent-notification-service")

PROVIDER_BASE_URL = os.getenv("PROVIDER_BASE_URL", "http://localhost:3001")
PROVIDER_API_KEY = os.getenv("PROVIDER_API_KEY", "test-dev-2026")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "6.0"))
MAX_AI_CONCURRENCY = int(os.getenv("MAX_AI_CONCURRENCY", "20"))

_ai_semaphore = asyncio.Semaphore(MAX_AI_CONCURRENCY)

SYSTEM_PROMPT = """You extract notification instructions from Spanish or English user text.
Return ONLY compact JSON with exactly these keys:
{"to":"destination email or phone", "message":"message to send", "type":"email" or "sms"}
Rules:
- Use type=email for correo, email, e-mail, mail, or an email destination.
- Use type=sms for SMS, teléfono, phone, móvil, or a phone destination.
- Do not include Markdown, explanations, confidence, or extra fields.
- If information is missing, return JSON null values for missing fields instead of refusing.
"""


async def extract_with_ai(user_input: str) -> str:
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]
    }
    async with _ai_semaphore:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    f"{PROVIDER_BASE_URL}/v1/ai/extract",
                    headers={"X-API-Key": PROVIDER_API_KEY, "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                logger.warning("AI extraction failed: %s", exc)
                return ""
    return body.get("choices", [{}])[0].get("message", {}).get("content", "")


def parse_ai_notification(content: str) -> NotificationRequest | None:
    for candidate in _candidate_json_strings(content):
        data = _loads_relaxed_json(candidate)
        if not isinstance(data, dict):
            continue
        normalized = _normalize_ai_payload(data)
        if normalized is None:
            continue
        return normalized
    return None


def _candidate_json_strings(content: str) -> list[str]:
    if not content:
        return []

    candidates: list[str] = []
    for block in re.findall(r"```(?:json)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL):
        candidates.append(block.strip())

    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(content[start : end + 1])

    stripped = content.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)

    return list(dict.fromkeys(candidates))


def _loads_relaxed_json(candidate: str) -> dict[str, Any] | None:
    text = candidate.strip()
    if not text.endswith("}") and "..." in text:
        text = text.split("...", 1)[0].rstrip().rstrip(",") + "}"
    text = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
    try:
        return json_loads(text)
    except ValueError:
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
        return value if isinstance(value, dict) else None


def json_loads(text: str) -> dict[str, Any]:
    import json

    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("AI response JSON must be an object")
    return value


def _normalize_ai_payload(data: dict[str, Any]) -> NotificationRequest | None:
    key_aliases = {
        "to": ("to", "recipient", "destination", "destino"),
        "message": ("message", "body", "text", "mensaje"),
        "type": ("type", "channel", "method", "tipo"),
    }
    lowered = {str(key).lower(): value for key, value in data.items()}
    normalized: dict[str, str] = {}
    for target_key, aliases in key_aliases.items():
        value = next((lowered[alias] for alias in aliases if alias in lowered), None)
        if value is None:
            return None
        normalized[target_key] = str(value).strip()

    normalized["type"] = _normalize_type(normalized["type"])
    try:
        return NotificationRequest(**normalized)
    except ValidationError:
        return None


def heuristic_extract_notification(user_input: str) -> NotificationRequest | None:
    text = user_input.strip()
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    phone_match = re.search(r"\b\d{3}-?\d{3}-?\d{3,4}\b", text)
    target = email_match.group(0) if email_match else (phone_match.group(0) if phone_match else None)
    if not target:
        return None

    notification_type = "email" if email_match else "sms"
    lowered = text.lower()
    if "sms" in lowered or "teléfono" in lowered or "telefono" in lowered or "phone" in lowered:
        notification_type = "sms"
    elif "mail" in lowered or "correo" in lowered or "email" in lowered:
        notification_type = "email"

    message = _extract_message_text(text, target)
    try:
        return NotificationRequest(to=target, message=message, type=notification_type)
    except ValidationError:
        return None


def _extract_message_text(text: str, target: str) -> str:
    after_colon = text.split(":", 1)[1].strip() if ":" in text else ""
    saying_match = re.search(r"(?:diciendo|indicando|mensaje(?:\s+de)?|saying)\s+(.+)$", text, flags=re.IGNORECASE)
    if after_colon:
        return after_colon[:500]
    if saying_match:
        return saying_match.group(1).strip()[:500]
    return text.replace(target, "").strip(" .:-")[:500] or "Notificación"


def _normalize_type(value: str) -> str:
    lowered = value.lower().strip()
    if lowered in {"email", "mail", "correo", "e-mail"}:
        return "email"
    if lowered in {"sms", "phone", "telefono", "teléfono", "movil", "móvil"}:
        return "sms"
    return lowered
