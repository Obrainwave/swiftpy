from __future__ import annotations

import hashlib
import hmac
import tempfile
import time
from urllib.parse import parse_qs, urlparse

from swiftpy.filesystem.drivers.local import LocalStorageDriver


async def test_signed_url_generation_and_verification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = LocalStorageDriver(root=tmp, signing_key="super-secret")

        signed_url = await driver.temporary_url("private/report.pdf", expires_in=60)
        parsed = urlparse(signed_url)
        params = parse_qs(parsed.query)

        assert "expires" in params
        assert "signature" in params

        expires_val = params["expires"][0]
        sig_val = params["signature"][0]

        # Verify signature mathematically
        payload = f"GET\nprivate/report.pdf\n{expires_val}"
        expected_sig = hmac.new(
            b"super-secret",
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert sig_val == expected_sig

        # Tampering with path must fail
        tampered_payload = f"GET\nprivate/other.pdf\n{expires_val}"
        tampered_sig = hmac.new(
            b"super-secret",
            tampered_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert sig_val != tampered_sig


async def test_signed_url_expiration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = LocalStorageDriver(root=tmp, signing_key="super-secret")
        # Expired 5 seconds ago
        signed_url = await driver.temporary_url("doc.pdf", expires_in=-5)
        parsed = urlparse(signed_url)
        params = parse_qs(parsed.query)

        expires_timestamp = int(params["expires"][0])
        assert expires_timestamp < time.time()

