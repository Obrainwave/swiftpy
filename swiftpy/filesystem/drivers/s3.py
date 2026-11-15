from __future__ import annotations

import asyncio
import hashlib
import hmac
import mimetypes
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, BinaryIO
from urllib.parse import quote, urlencode, urlparse

import httpx

from swiftpy.filesystem.drivers.base import StorageDriver
from swiftpy.filesystem.exceptions import FileNotFoundError
from swiftpy.filesystem.metadata import FileMetadata
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.visibility import Visibility

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_S3_SPOOL_THRESHOLD = 1024 * 1024  # 1 MiB bounded RAM threshold for streaming PUTs


def _create_s3_spool() -> tempfile.SpooledTemporaryFile[bytes]:
    return tempfile.SpooledTemporaryFile(max_size=_S3_SPOOL_THRESHOLD)


async def _iter_s3_spool(
    spool: tempfile.SpooledTemporaryFile[bytes],
    chunk_size: int,
) -> AsyncIterator[bytes]:
    await asyncio.to_thread(spool.seek, 0)
    while True:
        chunk = await asyncio.to_thread(spool.read, chunk_size)
        if not chunk:
            break
        yield chunk


def _get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
    k_date = hmac.new(b"AWS4" + key.encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region_name.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service_name.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


class S3StorageDriver(StorageDriver):
    """
    S3-compatible object storage driver supporting AWS S3, MinIO, Cloudflare R2,
    Ceph, and DigitalOcean Spaces via async HTTP with native AWS SigV4 authentication.
    """

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        endpoint: str | None = None,
        url: str | None = None,
        use_path_style_endpoint: bool = False,
        default_visibility: Visibility | str = Visibility.PRIVATE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.endpoint = endpoint
        self.public_url = url
        self.use_path_style = use_path_style_endpoint
        self.default_visibility = Visibility(default_visibility)

        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

        # Determine base URL host and path prefix
        if self.endpoint:
            parsed = urlparse(self.endpoint)
            self._scheme = parsed.scheme or "https"
            self._host = parsed.netloc
            self._base_path = parsed.path.rstrip("/")
        else:
            self._scheme = "https"
            self._host = f"{self.bucket}.s3.{self.region}.amazonaws.com" if not self.use_path_style else f"s3.{self.region}.amazonaws.com"
            self._base_path = ""

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> S3StorageDriver:
        bucket = str(config.get("bucket", ""))
        access_key = str(config.get("key", config.get("access_key", "")))
        secret_key = str(config.get("secret", config.get("secret_key", "")))
        region = str(config.get("region", "us-east-1"))
        endpoint = config.get("endpoint")
        url = config.get("url")
        use_path_style = bool(config.get("use_path_style_endpoint", False))
        visibility = str(config.get("visibility", "private"))

        return cls(
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            endpoint=str(endpoint) if endpoint else None,
            url=str(url) if url else None,
            use_path_style_endpoint=use_path_style,
            default_visibility=visibility,
        )

    def _get_key_path(self, key: str) -> str:
        clean = PathNormalizer.normalize(key)
        if self.use_path_style:
            resource = f"/{self.bucket}/{clean}" if clean else f"/{self.bucket}"
        else:
            resource = f"/{clean}" if clean else "/"
        if self._base_path:
            if resource == "/":
                return self._base_path
            return f"{self._base_path}{resource}"
        return resource

    def _build_url(self, key: str, query: str = "") -> str:
        path = quote(self._get_key_path(key), safe="/-_.~")
        url = f"{self._scheme}://{self._host}{path}"
        if query:
            url = f"{url}?{query}"
        return url

    def _sign_headers(
        self,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        payload_hash: str,
        timestamp: datetime | None = None,
    ) -> dict[str, str]:
        now = timestamp or datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        req_headers: dict[str, str] = {
            str(k).lower(): str(v).strip() for k, v in headers.items()
        }
        req_headers["host"] = self._host
        req_headers["x-amz-date"] = amz_date
        req_headers["x-amz-content-sha256"] = payload_hash

        # Canonical headers
        sorted_keys = sorted(req_headers)
        canonical_headers = "".join(f"{k}:{req_headers[k]}\n" for k in sorted_keys)
        signed_headers = ";".join(sorted_keys)

        canonical_uri = quote(path, safe="/-_.~")
        canonical_query = query

        canonical_request = (
            f"{method}\n{canonical_uri}\n{canonical_query}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        signing_key = _get_signature_key(self.secret_key, date_stamp, self.region, "s3")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        req_headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return req_headers

    @staticmethod
    def _parse_s3_error(body: str) -> str:
        """Extract S3 error Code and Message from XML response body."""
        try:
            root = ET.fromstring(body)
            code = ""
            message = ""
            for elem in root.iter():
                tag = elem.tag.split("}", 1)[-1]
                if tag == "Code" and elem.text:
                    code = elem.text.strip()
                elif tag == "Message" and elem.text:
                    message = elem.text.strip()
            if code and message:
                return f"{code}: {message}"
            if code or message:
                return code or message
        except (ET.ParseError, ValueError):
            return body
        return body

    @staticmethod
    def _parse_list_objects_v2(body: str) -> tuple[list[str], list[str], bool, str | None]:
        """
        Parse S3 ListObjectsV2 XML response.
        Returns: (keys, common_prefixes, is_truncated, next_continuation_token)
        Handles XML namespaces and entity unescaping automatically.
        """
        keys: list[str] = []
        prefixes: list[str] = []
        is_truncated = False
        next_continuation_token: str | None = None

        try:
            root = ET.fromstring(body)
            for elem in root.iter():
                tag = elem.tag.split("}", 1)[-1]
                if tag == "Key" and elem.text:
                    keys.append(elem.text)
                elif tag == "CommonPrefixes":
                    for sub in elem.iter():
                        sub_tag = sub.tag.split("}", 1)[-1]
                        if sub_tag == "Prefix" and sub.text:
                            prefixes.append(sub.text.rstrip("/"))
                elif tag == "IsTruncated" and elem.text:
                    is_truncated = elem.text.strip().lower() == "true"
                elif tag == "NextContinuationToken" and elem.text:
                    next_continuation_token = elem.text.strip()
        except (ET.ParseError, ValueError):
            return keys, prefixes, is_truncated, next_continuation_token

        return keys, prefixes, is_truncated, next_continuation_token

    async def write(
        self,
        path: str,
        content: bytes | str,
        visibility: Visibility | str | None = None,
    ) -> int:
        clean = PathNormalizer.normalize(path)
        data = content.encode("utf-8") if isinstance(content, str) else content
        payload_hash = hashlib.sha256(data).hexdigest()
        key_path = self._get_key_path(clean)

        vis = Visibility(visibility) if visibility is not None else self.default_visibility
        acl = "public-read" if vis == Visibility.PUBLIC else "private"
        mime, _ = mimetypes.guess_type(clean)

        headers: dict[str, str] = {
            "content-length": str(len(data)),
            "content-type": mime or "application/octet-stream",
            "x-amz-acl": acl,
        }

        signed_headers = self._sign_headers("PUT", key_path, "", headers, payload_hash)
        url = self._build_url(clean)

        resp = await self._client.put(url, headers=signed_headers, content=data)
        if resp.status_code == 403:
            raise PermissionError(f"S3 PUT denied for '{path}': {self._parse_s3_error(resp.text)}")
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"S3 PUT failed with status {resp.status_code}: {self._parse_s3_error(resp.text)}")
        return len(data)

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes] | BinaryIO,
        chunk_size: int = 64 * 1024,
        visibility: Visibility | str | None = None,
    ) -> int:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        vis = Visibility(visibility) if visibility is not None else self.default_visibility
        acl = "public-read" if vis == Visibility.PUBLIC else "private"
        mime, _ = mimetypes.guess_type(clean)

        spool = _create_s3_spool()
        hasher = hashlib.sha256()
        total = 0
        try:
            if isinstance(stream, AsyncIterator):
                async for chunk in stream:
                    if chunk:
                        total += len(chunk)
                        hasher.update(chunk)
                        await asyncio.to_thread(spool.write, chunk)
            else:
                while True:
                    chunk = await asyncio.to_thread(stream.read, chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    hasher.update(chunk)
                    await asyncio.to_thread(spool.write, chunk)

            payload_hash = hasher.hexdigest()
            headers = {
                "content-type": mime or "application/octet-stream",
                "x-amz-acl": acl,
                "content-length": str(total),
            }
            signed_headers = self._sign_headers("PUT", key_path, "", headers, payload_hash)
            url = self._build_url(clean)

            resp = await self._client.put(
                url,
                headers=signed_headers,
                content=_iter_s3_spool(spool, chunk_size),
            )
            if resp.status_code == 403:
                raise PermissionError(f"S3 PUT denied for '{path}': {self._parse_s3_error(resp.text)}")
            if resp.status_code not in (200, 201, 204):
                raise RuntimeError(f"S3 PUT failed with status {resp.status_code}: {self._parse_s3_error(resp.text)}")
            return total
        finally:
            await asyncio.to_thread(spool.close)

    async def read(self, path: str) -> bytes:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        signed_headers = self._sign_headers("GET", key_path, "", {}, _EMPTY_SHA256)
        url = self._build_url(clean)

        resp = await self._client.get(url, headers=signed_headers)
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found on S3 at '{path}'.")
        if resp.status_code == 403:
            raise PermissionError(f"S3 GET denied for '{path}': {self._parse_s3_error(resp.text)}")
        if resp.status_code != 200:
            raise RuntimeError(f"S3 GET failed with status {resp.status_code}: {self._parse_s3_error(resp.text)}")
        return resp.content

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        data = await self.read(path)
        return data.decode(encoding)

    async def read_stream(
        self,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        signed_headers = self._sign_headers("GET", key_path, "", {}, _EMPTY_SHA256)
        url = self._build_url(clean)

        req = self._client.build_request("GET", url, headers=signed_headers)
        resp = await self._client.send(req, stream=True)
        if resp.status_code == 404:
            await resp.aclose()
            raise FileNotFoundError(f"File not found on S3 at '{path}'.")
        if resp.status_code == 403:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await resp.aclose()
            raise PermissionError(f"S3 GET stream denied for '{path}': {self._parse_s3_error(text)}")
        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "ignore")
            await resp.aclose()
            raise RuntimeError(f"S3 GET stream failed with status {resp.status_code}: {self._parse_s3_error(text)}")

        try:
            async for chunk in resp.aiter_bytes(chunk_size):
                yield chunk
        finally:
            await resp.aclose()

    async def exists(self, path: str) -> bool:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        signed_headers = self._sign_headers("HEAD", key_path, "", {}, _EMPTY_SHA256)
        url = self._build_url(clean)

        resp = await self._client.head(url, headers=signed_headers)
        return resp.status_code == 200

    async def missing(self, path: str) -> bool:
        return not await self.exists(path)

    async def delete(self, path: str) -> bool:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        signed_headers = self._sign_headers("DELETE", key_path, "", {}, _EMPTY_SHA256)
        url = self._build_url(clean)

        resp = await self._client.delete(url, headers=signed_headers)
        return resp.status_code in (200, 204)

    async def copy(self, source: str, destination: str) -> bool:
        src = PathNormalizer.normalize(source)
        dst = PathNormalizer.normalize(destination)
        key_path = self._get_key_path(dst)

        headers = {
            "x-amz-copy-source": quote(f"/{self.bucket}/{src}"),
        }
        signed_headers = self._sign_headers("PUT", key_path, "", headers, _EMPTY_SHA256)
        url = self._build_url(dst)

        resp = await self._client.put(url, headers=signed_headers)
        if resp.status_code == 404:
            raise FileNotFoundError(f"Source file not found at '{source}'.")
        if resp.status_code == 403:
            raise PermissionError(f"S3 COPY denied for '{source}': {self._parse_s3_error(resp.text)}")
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"S3 COPY failed with status {resp.status_code}: {self._parse_s3_error(resp.text)}")
        return True

    async def move(self, source: str, destination: str) -> bool:
        await self.copy(source, destination)
        await self.delete(source)
        return True

    async def size(self, path: str) -> int:
        meta = await self.metadata(path)
        return meta.size

    async def mime_type(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        mime, _ = mimetypes.guess_type(clean)
        return mime or "application/octet-stream"

    async def last_modified(self, path: str) -> datetime:
        meta = await self.metadata(path)
        return meta.last_modified

    async def checksum(self, path: str, algorithm: str = "sha256") -> str:
        hasher = hashlib.new(algorithm)
        async for chunk in self.read_stream(path):
            hasher.update(chunk)
        return hasher.hexdigest()

    async def files(self, directory: str = "", recursive: bool = False) -> list[str]:
        clean = PathNormalizer.normalize(directory) if directory else ""
        prefix = f"{clean}/" if clean else ""
        results: list[str] = []
        continuation_token: str | None = None

        while True:
            query_params: dict[str, str] = {
                "list-type": "2",
                "prefix": prefix,
            }
            if not recursive:
                query_params["delimiter"] = "/"
            if continuation_token:
                query_params["continuation-token"] = continuation_token

            query_str = urlencode(sorted(query_params.items()))
            key_path = self._get_key_path("")
            signed_headers = self._sign_headers("GET", key_path, query_str, {}, _EMPTY_SHA256)
            url = self._build_url("", query=query_str)

            resp = await self._client.get(url, headers=signed_headers)
            if resp.status_code == 403:
                raise PermissionError(f"S3 list files denied for '{directory}': {self._parse_s3_error(resp.text)}")
            if resp.status_code != 200:
                raise RuntimeError(f"S3 list files failed with status {resp.status_code}: {self._parse_s3_error(resp.text)}")

            keys, _, is_truncated, next_token = self._parse_list_objects_v2(resp.text)
            for k in keys:
                if not k.endswith("/"):
                    results.append(k)

            if not is_truncated or not next_token:
                break
            continuation_token = next_token

        results.sort()
        return results

    async def directories(self, directory: str = "", recursive: bool = False) -> list[str]:
        clean = PathNormalizer.normalize(directory) if directory else ""
        prefix = f"{clean}/" if clean else ""
        results: set[str] = set()
        continuation_token: str | None = None

        while True:
            query_params: dict[str, str] = {
                "list-type": "2",
                "prefix": prefix,
            }
            if not recursive:
                query_params["delimiter"] = "/"
            if continuation_token:
                query_params["continuation-token"] = continuation_token

            query_str = urlencode(sorted(query_params.items()))
            key_path = self._get_key_path("")
            signed_headers = self._sign_headers("GET", key_path, query_str, {}, _EMPTY_SHA256)
            url = self._build_url("", query=query_str)

            resp = await self._client.get(url, headers=signed_headers)
            if resp.status_code == 403:
                raise PermissionError(f"S3 list directories denied for '{directory}': {self._parse_s3_error(resp.text)}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"S3 list directories failed with status {resp.status_code}: {self._parse_s3_error(resp.text)}"
                )

            keys, prefixes, is_truncated, next_token = self._parse_list_objects_v2(resp.text)
            for p in prefixes:
                if p:
                    results.add(p)

            if recursive:
                for k in keys:
                    rel = k[len(prefix):] if prefix and k.startswith(prefix) else k
                    parts = rel.split("/")
                    if len(parts) > 1:
                        accum = prefix
                        for p in parts[:-1]:
                            accum = f"{accum}{p}"
                            results.add(accum.rstrip("/"))
                            accum = f"{accum}/"

            if not is_truncated or not next_token:
                break
            continuation_token = next_token

        sorted_results = sorted(results)
        return sorted_results

    async def make_directory(self, directory: str) -> bool:
        clean = PathNormalizer.normalize(directory)
        if clean:
            # S3 directory placeholder is an empty object ending in '/'
            await self.write(f"{clean}/", b"")
        return True

    async def delete_directory(self, directory: str) -> bool:
        all_files = await self.files(directory, recursive=True)
        for f in all_files:
            await self.delete(f)
        clean = PathNormalizer.normalize(directory)
        if clean:
            await self.delete(f"{clean}/")
        return True

    async def url(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        if self.public_url:
            encoded = quote(clean, safe="/-_.~")
            return f"{self.public_url.rstrip('/')}/{encoded}"
        return self._build_url(clean)

    async def temporary_url(
        self,
        path: str,
        expires_in: int | timedelta,
        timestamp: datetime | None = None,
    ) -> str:
        if isinstance(expires_in, bool):
            raise TypeError("expires_in must be an integer or timedelta, not bool.")
        clean = PathNormalizer.normalize(path)
        duration = (
            int(expires_in)
            if isinstance(expires_in, (int, float))
            else int(expires_in.total_seconds())
        )
        if duration <= 0:
            raise ValueError("expires_in must be greater than 0 seconds.")
        now = timestamp or datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"

        query_params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{credential_scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(duration),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = urlencode(sorted(query_params.items()))
        key_path = self._get_key_path(clean)
        encoded_key_path = quote(key_path, safe="/-_.~")

        canonical_headers = f"host:{self._host}\n"
        canonical_request = (
            f"GET\n{encoded_key_path}\n{canonical_query}\n"
            f"{canonical_headers}\nhost\nUNSIGNED-PAYLOAD"
        )

        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        signing_key = _get_signature_key(self.secret_key, date_stamp, self.region, "s3")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        final_query = f"{canonical_query}&X-Amz-Signature={signature}"
        return f"{self._scheme}://{self._host}{encoded_key_path}?{final_query}"

    async def get_visibility(self, path: str) -> Visibility:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        signed_headers = self._sign_headers("GET", key_path, "acl", {}, _EMPTY_SHA256)
        url = self._build_url(clean, query="acl")

        resp = await self._client.get(url, headers=signed_headers)
        if resp.status_code == 200 and "AllUsers" in resp.text and "READ" in resp.text:
            return Visibility.PUBLIC
        return Visibility.PRIVATE

    async def set_visibility(self, path: str, visibility: Visibility | str) -> bool:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        vis = Visibility(visibility)
        acl = "public-read" if vis == Visibility.PUBLIC else "private"

        headers = {"x-amz-acl": acl}
        signed_headers = self._sign_headers("PUT", key_path, "acl", headers, _EMPTY_SHA256)
        url = self._build_url(clean, query="acl")

        resp = await self._client.put(url, headers=signed_headers)
        return resp.status_code == 200

    async def metadata(self, path: str) -> FileMetadata:
        clean = PathNormalizer.normalize(path)
        key_path = self._get_key_path(clean)
        signed_headers = self._sign_headers("HEAD", key_path, "", {}, _EMPTY_SHA256)
        url = self._build_url(clean)

        resp = await self._client.head(url, headers=signed_headers)
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found on S3 at '{path}'.")
        if resp.status_code != 200:
            raise RuntimeError(f"S3 HEAD failed with status {resp.status_code}")

        size = int(resp.headers.get("content-length", "0"))
        mime = resp.headers.get("content-type") or "application/octet-stream"
        etag = resp.headers.get("etag", "").strip('"')

        last_mod_header = resp.headers.get("last-modified")
        if last_mod_header:
            try:
                last_mod = parsedate_to_datetime(last_mod_header).astimezone(UTC)
            except (ValueError, TypeError, OverflowError):
                last_mod = datetime.now(UTC)
        else:
            last_mod = datetime.now(UTC)

        return FileMetadata(
            path=clean,
            size=size,
            mime_type=mime,
            last_modified=last_mod,
            visibility=self.default_visibility,
            etag=etag or None,
        )

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()
