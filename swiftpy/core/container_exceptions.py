class ContainerError(Exception):
    """Base exception for all container-related errors."""


class ResolutionError(ContainerError):
    """Raised when the container fails to resolve a dependency or auto-wire a target."""


class CircularDependencyError(ResolutionError):
    """Raised when a circular dependency graph is detected during resolution."""


class BindingNotFoundError(ResolutionError):
    """Raised when an interface has no registered binding and cannot be auto-wired."""


class PrimitiveResolutionError(ResolutionError):
    """Raised when trying to auto-wire primitive built-in types."""
