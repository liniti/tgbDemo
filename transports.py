"""Отправка заявок в Telegram и webhook, без внешних зависимостей."""

from __future__ import annotations

import json
import http.client
import urllib.error
import urllib.request
from typing import Any, Mapping


_TIMEOUT_SECONDS = 10
_TELEGRAM_TEXT_LIMIT = 4096
_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    OSError,
    ValueError,
    http.client.HTTPException,
)


class TransportError(RuntimeError):
    """Ошибка отправки, которую безопасно показывать: без токенов и адресов."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Не пересылает тело запроса и учётные данные на адрес перенаправления."""

    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


def telegram_text(item: Mapping[str, Any]) -> str:
    """Собирает текст уведомления и обрезает его до лимита Telegram."""
    title = str(item.get("title") or "Без названия")
    title = title.replace("\r", " ").replace("\n", " ").strip()
    amount = str(item.get("amount", "—"))
    currency = str(item.get("currency", "")).strip()
    created_at = str(item.get("created_at", "—"))
    item_id = str(item.get("id", "—"))
    
    money = f"{amount} {currency}".strip()
    
    message = (
        "Новая заявка\n"
        f"Название: {title}\n"
        f"Сумма: {money}\n"
        f"Создана: {created_at}\n"
        f"ID: {item_id}"
    )
    if len(message) > _TELEGRAM_TEXT_LIMIT:
        message = message[: _TELEGRAM_TEXT_LIMIT - 3].rstrip() + "..."
    return message


def send_telegram(item: Mapping[str, Any], token: str, chat_id: str) -> None:
    """Отправляет заявку через sendMessage Telegram Bot API.

    Тексты ошибок намеренно общие: токен входит в URL запроса, поэтому ни адрес,
    ни исходный текст исключения наружу не попадают.
    """
    if not token or not chat_id:
        raise TransportError("Для Telegram нужны токен бота и chat_id.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {"chat_id": chat_id, "text": telegram_text(item)},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _opener().open(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TransportError(f"Telegram вернул HTTP {exc.code}.") from None
    except _NETWORK_ERRORS:
        raise TransportError("Не удалось связаться с Telegram.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TransportError("Telegram вернул некорректный ответ.") from None

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        
        code = payload.get("error_code") if isinstance(payload, dict) else None
        suffix = f" (код {code})" if isinstance(code, int) else ""
        
        raise TransportError(f"Telegram отклонил сообщение{suffix}.")


def send_webhook(item: Mapping[str, Any], url: str) -> None:
    """Отправляет заявку POST-запросом с Idempotency-Key; успехом считает любой 2xx."""
    item_id = str(item.get("id", ""))
    if not item_id or any(ord(char) < 32 or ord(char) == 127 for char in item_id):
        raise TransportError("У заявки должен быть корректный id для Idempotency-Key.")

    try:
        body = json.dumps(item, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise TransportError("Заявку не удалось преобразовать в JSON.") from None

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": item_id,
        },
        method="POST",
    )
    
    try:
        with _opener().open(request, timeout=_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        raise TransportError(
            f"Webhook вернул HTTP {exc.code}; адресат не подтвердил приём."
        ) from None
    except _NETWORK_ERRORS:
        raise TransportError("Не удалось связаться с внутренним webhook.") from None

    if not 200 <= status < 300:
        raise TransportError(
            f"Webhook вернул HTTP {status}; адресат не подтвердил приём."
        )
