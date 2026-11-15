from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from swiftpy.filesystem.drivers.s3 import S3StorageDriver
from swiftpy.filesystem.exceptions import FileNotFoundError


@pytest.mark.asyncio
async def test_s3_sigv4_cryptographic_verification_against_reference_vector() -> None:
    """
    Cryptographically verify S3 SigV4 signature against an independently calculated
    AWS Signature Version 4 reference test vector.
    """
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    key = "AKIAIOSFODNN7EXAMPLE"
    region = "us-east-1"
    bucket = "my-bucket"
    dt = datetime(2013, 5, 24, 0, 0, 0, tzinfo=UTC)

    driver = S3StorageDriver(
        bucket=bucket,
        access_key=key,
        secret_key=secret,
        region=region,
    )

    try:
        payload_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        signed_headers = driver._sign_headers(
            method="GET",
            path="/test.txt",
            query="",
            headers={},
            payload_hash=payload_hash,
            timestamp=dt,
        )

        # Independent calculation
        amz_date = "20130524T000000Z"
        date_stamp = "20130524"
        host = f"{bucket}.s3.{region}.amazonaws.com"
        canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed_headers_list = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = f"GET\n/test.txt\n\n{canonical_headers}\n{signed_headers_list}\n{payload_hash}"
        hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        scope = f"{date_stamp}/{region}/s3/aws4_request"
        string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashed_canonical}"

        k_date = hmac.new(b"AWS4" + secret.encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
        k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        expected_sig = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        expected_auth = (
            f"AWS4-HMAC-SHA256 Credential={key}/{scope}, "
            f"SignedHeaders={signed_headers_list}, Signature={expected_sig}"
        )

        assert signed_headers["authorization"] == expected_auth
        assert signed_headers["host"] == host
        assert signed_headers["x-amz-date"] == amz_date
        assert signed_headers["x-amz-content-sha256"] == payload_hash
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_s3_presigned_url_query_parameters() -> None:
    driver = S3StorageDriver(
        bucket="my-bucket",
        access_key="MY_KEY",
        secret_key="MY_SECRET",
        region="eu-west-1",
    )

    try:
        dt = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
        presigned = await driver.temporary_url("reports/annual.pdf", expires_in=3600, timestamp=dt)
        parsed = urlparse(presigned)
        params = parse_qs(parsed.query)

        assert params["X-Amz-Algorithm"][0] == "AWS4-HMAC-SHA256"
        assert params["X-Amz-Credential"][0] == "MY_KEY/20261110/eu-west-1/s3/aws4_request"
        assert params["X-Amz-Date"][0] == "20261110T120000Z"
        assert params["X-Amz-Expires"][0] == "3600"
        assert params["X-Amz-SignedHeaders"][0] == "host"
        assert "X-Amz-Signature" in params
        assert len(params["X-Amz-Signature"][0]) == 64
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_s3_realistic_xml_namespaces_entities_and_pagination() -> None:
    """
    Test realistic AWS S3 responses including:
    - Official S3 XML namespace (xmlns="http://s3.amazonaws.com/doc/2006-03-01/")
    - XML entity escaping (&amp;, &lt;, &gt;)
    - Multi-page pagination with continuation-token
    """
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        params = dict(request.url.params)
        assert params.get("list-type") == "2"

        token = params.get("continuation-token")
        if not token:
            # Page 1: truncated, returns next continuation token
            xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>xml-bucket</Name>
    <Prefix></Prefix>
    <KeyCount>2</KeyCount>
    <MaxKeys>2</MaxKeys>
    <IsTruncated>true</IsTruncated>
    <NextContinuationToken>page2-token-xyz</NextContinuationToken>
    <Contents>
        <Key>docs/file &amp; folder &lt;1&gt;.pdf</Key>
        <Size>1234</Size>
    </Contents>
    <Contents>
        <Key>docs/regular.txt</Key>
        <Size>567</Size>
    </Contents>
</ListBucketResult>"""
            return httpx.Response(200, text=xml)
        elif token == "page2-token-xyz":
            # Page 2: final page
            xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Name>xml-bucket</Name>
    <Prefix></Prefix>
    <KeyCount>1</KeyCount>
    <MaxKeys>2</MaxKeys>
    <IsTruncated>false</IsTruncated>
    <Contents>
        <Key>docs/final_report.docx</Key>
        <Size>890</Size>
    </Contents>
</ListBucketResult>"""
            return httpx.Response(200, text=xml)
        return httpx.Response(400)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)
    driver = S3StorageDriver(
        bucket="xml-bucket",
        access_key="key",
        secret_key="secret",
        client=client,
    )

    try:
        files = await driver.files(recursive=True)
        assert len(files) == 3
        # Verify XML entity unescaping (&amp; -> &, &lt; -> <, &gt; -> >)
        assert "docs/file & folder <1>.pdf" in files
        assert "docs/regular.txt" in files
        assert "docs/final_report.docx" in files
    finally:
        await driver.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_s3_error_responses_handled_predictably() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "forbidden.txt" in path:
            xml = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
    <Code>AccessDenied</Code>
    <Message>Access Denied by bucket policy</Message>
    <Resource>/error-bucket/forbidden.txt</Resource>
</Error>"""
            return httpx.Response(403, text=xml)
        elif "missing.txt" in path:
            xml = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
    <Code>NoSuchKey</Code>
    <Message>The specified key does not exist.</Message>
</Error>"""
            return httpx.Response(404, text=xml)
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)
    driver = S3StorageDriver(
        bucket="error-bucket",
        access_key="key",
        secret_key="secret",
        client=client,
    )

    try:
        with pytest.raises(PermissionError) as exc_info:
            await driver.read("forbidden.txt")
        assert "AccessDenied: Access Denied by bucket policy" in str(exc_info.value)

        with pytest.raises(FileNotFoundError):
            await driver.read("missing.txt")
    finally:
        await driver.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_s3_crud_operations_mock_transport() -> None:
    storage_state: dict[str, bytes] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if request.method == "PUT":
            if "x-amz-copy-source" in request.headers:
                src_key = request.headers["x-amz-copy-source"].lstrip("/")
                if "/" in src_key:
                    src_key = src_key.split("/", 1)[1]
                if src_key in storage_state:
                    storage_state[key] = storage_state[src_key]
                    return httpx.Response(200)
                return httpx.Response(404)
            storage_state[key] = request.content
            return httpx.Response(200)
        elif request.method == "GET":
            if request.url.params.get("list-type") == "2":
                items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in storage_state)
                xml_body = f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>{items_xml}</ListBucketResult>'
                return httpx.Response(200, text=xml_body)
            if key in storage_state:
                return httpx.Response(200, content=storage_state[key], headers={"content-length": str(len(storage_state[key]))})
            return httpx.Response(404)
        elif request.method == "HEAD":
            if key in storage_state:
                return httpx.Response(200, headers={"content-length": str(len(storage_state[key])), "etag": '"abc"'})
            return httpx.Response(404)
        elif request.method == "DELETE":
            storage_state.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    driver = S3StorageDriver(
        bucket="test-bucket",
        access_key="test_key",
        secret_key="test_secret",
        client=client,
    )

    try:
        # Write
        written = await driver.write("doc.txt", b"s3 content")
        assert written == 10

        # Exists & Read
        assert await driver.exists("doc.txt")
        data = await driver.read("doc.txt")
        assert data == b"s3 content"

        # Copy
        await driver.copy("doc.txt", "doc_copy.txt")
        assert await driver.exists("doc_copy.txt")

        # Files listing
        files = await driver.files()
        assert "doc.txt" in files
        assert "doc_copy.txt" in files

        # Delete
        deleted = await driver.delete("doc.txt")
        assert deleted is True
        assert not await driver.exists("doc.txt")
    finally:
        await driver.close()
        await client.aclose()
