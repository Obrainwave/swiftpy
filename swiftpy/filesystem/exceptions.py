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


class InvalidMimeTypeError(FileValidationError):
    """Raised when an uploaded file's MIME type does not match allowed types."""

    def __init__(
        self,
        errors_or_message: str | dict[str, list[str]] = "Invalid file MIME type.",
        message: str | None = None,
        headers: dict[str, str] | None = None,
        field_name: str = "file",
    ) -> None:
        errors, resolved_msg = _normalize_validation_args(
            errors_or_message,
            message=message,
            default_message="Invalid file MIME type.",
            field_name=field_name,
        )
        super().__init__(errors=errors, message=resolved_msg, headers=headers)


class InvalidFileExtensionError(FileValidationError):
    """Raised when an uploaded file's extension is disallowed."""

    def __init__(
        self,
        errors_or_message: str | dict[str, list[str]] = "Invalid file extension.",
        message: str | None = None,
        headers: dict[str, str] | None = None,
        field_name: str = "file",
    ) -> None:
        errors, resolved_msg = _normalize_validation_args(
            errors_or_message,
            message=message,
            default_message="Invalid file extension.",
            field_name=field_name,
        )
        super().__init__(errors=errors, message=resolved_msg, headers=headers)


def _normalize_validation_args(
    errors_or_message: str | dict[str, list[str]],
    *,
    message: str | None,
    default_message: str,
    field_name: str,
) -> tuple[dict[str, list[str]], str]:
    if isinstance(errors_or_message, str):
        resolved_msg = message or errors_or_message
        return {field_name: [resolved_msg]}, resolved_msg
    first_msg = next(
        (msgs[0] for msgs in errors_or_message.values() if msgs),
        default_message,
    )
    return errors_or_message, message or first_msg


class MalformedMultipartError(FileUploadError, HttpException):
    """Raised when multipart form body is corrupted or improperly formatted."""

    def __init__(self, message: str = "Malformed multipart request payload") -> None:
        super().__init__(message=message, status_code=400)
