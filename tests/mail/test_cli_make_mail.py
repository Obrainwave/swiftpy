from __future__ import annotations

import argparse
from pathlib import Path

from swiftpy.console.commands.make import MakeCommand


def test_make_mail_scaffold(tmp_path: Path) -> None:
    target_dir = tmp_path / "app" / "mail"
    args = argparse.Namespace(
        name="UserVerificationEmail",
        path=str(target_dir),
        force=False,
    )
    MakeCommand.handle_mail(args)

    expected_file = target_dir / "user_verification_email.py"
    assert expected_file.is_file()

    content = expected_file.read_text(encoding="utf-8")
    assert "class UserVerificationEmail(Mailable):" in content
    assert 'subject="UserVerificationEmail"' in content
