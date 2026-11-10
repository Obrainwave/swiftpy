from __future__ import annotations

import builtins

from swiftpy.http.exceptions import HttpException, PayloadTooLargeError


class FilesystemException(Exception):
    """Base exception for all filesystem and storage operations."""


class DiskNotFoundError(FilesystemException, KeyError):
    """Raised when an unconfigured disk is requested."""


class FileNotFoundError(FilesystemException, builtins.FileNotFoundError):
    """Raised when a requested file does not exist on storage."""


class FileAlreadyExistsError(FilesystemException, FileExistsError):
    """Raised when a file already exists and overwrite is false."""


class PathTraversalError(FilesystemException, PermissionError):
    """Raised when a path traversal attempt is detected."""


class ReadOnlyFileSystemError(FilesystemException, PermissionError):
    """Raised when a write or delete operation is attempted on a read-only disk."""


class StorageQuotaExceededError(FilesystemException):
    """Raised when storage disk quota or size threshold is exceeded."""


class InvalidSignatureError(FilesystemException):
    """Raised when a signed temporary URL has an invalid signature or is expired."""


class DriverNotSupportedError(FilesystemException):
    """Raised when a driver name is unknown or an operation is unsupported."""


class FileUploadError(FilesystemException):
    """Base exception for HTTP upload operations."""


class FileValidationError(FileUploadError, HttpException):
    """Raised when file upload validation fails. Defaults to HTTP 422."""

    def __init__(
        self,
        errors: dict[str, list[str]],
        message: str = "The uploaded file was invalid.",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.errors = errors
        super().__init__(message=message, status_code=422, headers=headers)


class FileTooLargeError(FileUploadError, PayloadTooLargeError):
    """Raised when an uploaded file or total request exceeds configured size limits."""

    def __init__(self, message: str = "Uploaded file payload is too large") -> None:
        super().__init__(message=message)


class InvalidMimeTypeError(FileUploadError):
    """Raised when an uploaded file's MIME type does not match allowed types."""


class InvalidFileExtensionError(FileUploadError):
    """Raised when an uploaded file's extension is disallowed."""


class MalformedMultipartError(FileUploadError, HttpException):
    """Raised when multipart form body is corrupted or improperly formatted."""

    def __init__(self, message: str = "Malformed multipart request payload") -> None:
        super().__init__(message=message, status_code=400)
