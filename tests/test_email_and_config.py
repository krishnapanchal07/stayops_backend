from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.services.email_service import send_reset_email


def test_starttls_reset_email(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from_email", "stayops@example.com")
    monkeypatch.setattr(settings, "smtp_username", "smtp-user")
    smtp = MagicMock()
    with patch("app.services.email_service.smtplib.SMTP", return_value=smtp):
        send_reset_email("tenant@example.com", "https://stayops.example/reset-password#token=private")
    connection = smtp.__enter__.return_value
    connection.starttls.assert_called_once()
    connection.login.assert_called_once()
    message = connection.send_message.call_args.args[0]
    assert message["Subject"] == "Reset Your StayOps Password"
    assert message["To"] == "tenant@example.com"
    assert "expires" in message.get_content() and "ignore" in message.get_content()


@pytest.mark.parametrize("overrides", [
    {"jwt_secret_key": "short"}, {"jwt_secret_key": "<REPLACE_WITH_RANDOM_SECRET_AT_LEAST_32_CHARACTERS>"},
    {"cors_origins": ["*"]}, {"cookie_samesite": "none", "cookie_secure": False},
    {"app_env": "production", "cookie_secure": False},
    {"jwt_algorithm": "none"},
])
def test_settings_reject_insecure_configuration(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
