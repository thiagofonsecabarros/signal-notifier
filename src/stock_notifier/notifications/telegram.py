from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class TelegramDeliveryResult:
    ok: bool
    request: dict[str, Any]
    response: dict[str, Any]
    error_text: str = ""


class TelegramClient:
    def __init__(self, bot_token: str, *, timeout_seconds: float = 30.0) -> None:
        self.bot_token = bot_token.strip()
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> TelegramDeliveryResult:
        if not self.bot_token:
            return TelegramDeliveryResult(
                ok=False,
                request={},
                response={},
                error_text="TELEGRAM_BOT_TOKEN is not configured",
            )
        if not str(chat_id).strip():
            return TelegramDeliveryResult(
                ok=False,
                request={},
                response={},
                error_text="TELEGRAM_CHAT_ID is not configured",
            )

        payload = {
            "chat_id": str(chat_id).strip(),
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            response = requests.post(url, json=payload, timeout=self.timeout_seconds)
            body = response.json() if response.content else {}
            if response.ok and bool(body.get("ok", False)):
                return TelegramDeliveryResult(ok=True, request=payload, response=body)
            return TelegramDeliveryResult(
                ok=False,
                request=payload,
                response=body,
                error_text=f"Telegram HTTP {response.status_code}: {body}",
            )
        except requests.RequestException as exc:
            return TelegramDeliveryResult(
                ok=False,
                request=payload,
                response={},
                error_text=repr(exc),
            )
