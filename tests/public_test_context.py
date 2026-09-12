"""Synthetic configuration for offline tests and acceptance contracts."""
from pathlib import Path
import os
from unittest.mock import patch


def install():
    fixtures = Path(__file__).parent / "fixtures"
    root = Path(__file__).resolve().parents[1]
    real_read_text, real_is_file = Path.read_text, Path.is_file

    def synthetic_config(path, *args, **kwargs):
        if path.resolve() == root / "config.yaml":
            return real_read_text(fixtures / "config.yaml", *args, **kwargs)
        return real_read_text(path, *args, **kwargs)

    def exclude_dotenv(path):
        return False if path.resolve() == root / ".env" else real_is_file(path)

    blank_env = {key: "" for key in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
        "RAPIDAPI_KEY", "EMAIL_SMTP_USER", "EMAIL_APP_PASSWORD", "EMAIL_TO",
        "IMAP_USER", "IMAP_APP_PASSWORD", "SERPER_API_KEY", "GEMINI_API_KEY",
    )}
    # Redirect before module singletons are constructed. A developer's invalid
    # config or env must never reach a validation traceback during collection.
    with patch.object(Path, "read_text", synthetic_config), patch.object(Path, "is_file", exclude_dotenv), patch.dict(os.environ, blank_env):
        from jobhound.config import CONFIG, load_config
        from jobhound.settings import settings

    frozen = load_config(fixtures / "config.yaml")
    # Mutate the existing object; imported references must see the same policy.
    for name in type(CONFIG).model_fields:
        setattr(CONFIG, name, getattr(frozen, name))
    CONFIG.trust.registry_file = str(fixtures / "platform_registry.yaml")
    # Import consumers after configuring: some legacy helpers bind CONFIG
    # children as default arguments at import time.
    from jobhound.filters import eligibility
    from jobhound.trust import registry
    from jobhound import join
    eligibility._PROFILE_PATH = fixtures / "profile.yaml"
    eligibility.default_profile.cache_clear()
    registry.clear_caches()
    join._DEFAULT_PATH = fixtures / "platforms_to_join.yaml"
    for name in type(settings).model_fields:
        setattr(settings, name, "")
