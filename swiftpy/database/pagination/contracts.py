from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, TypeVar

T_co = TypeVar("T_co", covariant=True)


class PaginatorContract[T_co](Protocol):
    items: list[T_co]
    total: int
    per_page: int
    current_page: int
    last_page: int

    @property
    def has_more_pages(self) -> bool: ...

    @property
    def has_previous_page(self) -> bool: ...

    @property
    def first_item(self) -> int | None: ...

    @property
    def last_item(self) -> int | None: ...

    def to_dict(self) -> dict[str, Any]: ...

    def __iter__(self) -> Iterator[T_co]: ...

    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> T_co: ...


class SimplePaginatorContract[T_co](Protocol):
    items: list[T_co]
    per_page: int
    current_page: int

    @property
    def has_more_pages(self) -> bool: ...

    @property
    def has_previous_page(self) -> bool: ...

    @property
    def first_item(self) -> int | None: ...

    @property
    def last_item(self) -> int | None: ...

    def to_dict(self) -> dict[str, Any]: ...

    def __iter__(self) -> Iterator[T_co]: ...

    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> T_co: ...
