"""Set dummy env so config import succeeds during tests (no real secrets)."""
import os

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "test_hash")
os.environ.setdefault("TELEGRAM_SESSION", "test")
os.environ.setdefault("CONTROL_BOT_TOKEN", "test:token")
os.environ.setdefault("ALLOWED_USER_ID", "1")
os.environ.setdefault("BYBIT_API_KEY", "k")
os.environ.setdefault("BYBIT_API_SECRET", "s")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("MODE", "DRY_RUN")
