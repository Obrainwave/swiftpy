from __future__ import annotations

import pytest

from swiftpy.core.config import Config
from swiftpy.mail.config import MailConfig
from swiftpy.mail.exceptions import MailConfigurationError, MailerNotFoundError
from swiftpy.mail.manager import MailManager
from swiftpy.mail.message import MailMessage
from swiftpy.mail.models import MailSendResult
from swiftpy.mail.transports.base import MailTransport
from swiftpy.mail.transports.console import ConsoleTransport


def test_mail_config_defaults() -> None:
    cfg = MailConfig()
    assert cfg.default == "console"
    assert cfg.from_address == "hello@example.com"
    assert cfg.from_name == "SwiftPY Application"


def test_mail_config_from_dict() -> None:
    raw = {
        "default": "smtp",
        "from": {"address": "test@domain.com", "name": "Test App"},
        "reply_to": {"address": "reply@domain.com", "name": "Replies"},
        "mailers": {"custom": {"transport": "console"}},
        "limits": {"max_attachment_size_bytes": 1024, "max_message_size_bytes": 2048},
    }
    cfg = MailConfig.from_dict(raw)
    assert cfg.default == "smtp"
    assert cfg.from_address == "test@domain.com"
    assert cfg.from_name == "Test App"
    assert cfg.reply_to_address == "reply@domain.com"
    assert cfg.max_attachment_size_bytes == 1024
    assert cfg.max_message_size_bytes == 2048


def test_mail_config_invalid_dict() -> None:
    with pytest.raises(MailConfigurationError):
        MailConfig.from_dict("not-a-dict")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mail_manager_default_resolution() -> None:
    config = Config(
        {
            "mail": {
                "default": "console",
                "mailers": {
                    "console": {"transport": "console"},
                },
            }
        }
    )
    manager = MailManager(config=config)
    transport = manager.mailer()
    assert isinstance(transport, ConsoleTransport)


@pytest.mark.asyncio
async def test_mail_manager_named_mailer() -> None:
    config = Config(
        {
            "mail": {
                "default": "console",
                "mailers": {
                    "console": {"transport": "console"},
                    "dev_log": {"transport": "console", "channel": "dev"},
                },
            }
        }
    )
    manager = MailManager(config=config)
    transport = manager.mailer("dev_log")
    assert isinstance(transport, ConsoleTransport)
    assert transport.channel == "dev"


@pytest.mark.asyncio
async def test_mail_manager_unconfigured_mailer_raises() -> None:
    config = Config({"mail": {"mailers": {}}})
    manager = MailManager(config=config)
    with pytest.raises(MailerNotFoundError):
        manager.mailer("non_existent")


@pytest.mark.asyncio
async def test_mail_manager_extend() -> None:
    class CustomTransport(MailTransport):
        async def send(self, message: MailMessage) -> MailSendResult:
            raise NotImplementedError

    config = Config(
        {
            "mail": {
                "default": "custom",
                "mailers": {
                    "custom": {"transport": "my_custom_driver"},
                },
            }
        }
    )
    manager = MailManager(config=config)
    manager.extend("my_custom_driver", lambda cfg: CustomTransport())

    transport = manager.mailer("custom")
    assert isinstance(transport, CustomTransport)


@pytest.mark.asyncio
async def test_mail_manager_send_applies_default_from() -> None:
    config = Config(
        {
            "mail": {
                "default": "console",
                "from": {"address": "global@example.com", "name": "Global Sender"},
                "mailers": {
                    "console": {"transport": "console"},
                },
            }
        }
    )
    manager = MailManager(config=config)
    msg = MailMessage(subject="Hello", to=["user@example.com"])
    result = await manager.send(msg)

    assert result.success is True
    assert msg.from_address == "global@example.com"
    assert msg.from_name == "Global Sender"

    await manager.close()
