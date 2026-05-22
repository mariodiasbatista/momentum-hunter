"""
Global test fixtures. Prevents accidental Telegram sends from any test.
TelegramHandler calls _send_safe in a background thread — patch it globally
so no test can ever fire a real message regardless of handler attachment.
"""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_telegram_sends():
    with patch("notifier.telegram_handler._send_safe"):
        yield
