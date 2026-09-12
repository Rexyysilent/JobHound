"""Secrets, loaded from .env (never config.yaml).

The .env path is anchored to the repo root, NOT the working directory — the
Task Scheduler launches with cwd=system32 and would otherwise silently run
keyless.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    # Telegram push-only digest delivery.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Email digest delivery (Gmail SMTP app password — NOT the account password).
    email_smtp_user: str = ""
    email_app_password: str = ""
    email_to: str = ""             # defaults to email_smtp_user when blank
    # Email-offer ingestion (v3 Patch #2). Blank = reuse the SMTP creds above —
    # a Gmail app password covers both SMTP and IMAP, so zero extra setup.
    imap_user: str = ""
    imap_app_password: str = ""
    # Tier A keyed sources (HANDOFF §5).
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    rapidapi_key: str = ""
    # P3 extras — features gate themselves off when these are absent.
    serper_api_key: str = ""       # SERP dorking (§11)
    gemini_api_key: str = ""       # LLM scam second-pass (§7a)

    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id and self.telegram_chat_id != "0")

    @property
    def email_ready(self) -> bool:
        return bool(self.email_smtp_user and self.email_app_password)

    @property
    def imap_user_effective(self) -> str:
        return self.imap_user or self.email_smtp_user

    @property
    def imap_password_effective(self) -> str:
        # Creds fall back as a PAIR: a different IMAP mailbox needs its own app
        # password — never try the SMTP account's password against it.
        if self.imap_user and self.imap_user != self.email_smtp_user:
            return self.imap_app_password
        return self.imap_app_password or self.email_app_password

    @property
    def imap_ready(self) -> bool:
        return bool(self.imap_user_effective and self.imap_password_effective)

    @property
    def adzuna_ready(self) -> bool:
        return bool(self.adzuna_app_id and self.adzuna_app_key)

    @property
    def jsearch_ready(self) -> bool:
        return bool(self.rapidapi_key)


settings = Settings()
