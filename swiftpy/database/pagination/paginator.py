from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any


class Paginator[T]:
    """
    Length-aware paginator presenting a paginated slice of records along with
    navigation metadata, total counts, and serialization helpers.
    """

    def __init__(
        self,
        items: list[T],
        total: int,
        per_page: int,
        current_page: int,
        last_page: int | None = None,
    ) -> None:
        if isinstance(total, bool) or not isinstance(total, int):
            raise TypeError("total must be an integer.")
        if total < 0:
            raise ValueError("total cannot be negative.")

        if isinstance(per_page, bool) or not isinstance(per_page, int):
            raise TypeError("per_page must be an integer.")
        if per_page < 1:
            raise ValueError("per_page must be >= 1.")

        if isinstance(current_page, bool) or not isinstance(current_page, int):
            raise TypeError("current_page must be an integer.")
        if current_page < 1:
            raise ValueError("current_page must be >= 1.")

        self.items: list[T] = items
        self.total: int = total
        self.per_page: int = per_page
        self.current_page: int = current_page

        if last_page is None:
            self.last_page = max(1, math.ceil(total / per_page)) if per_page > 0 else 1
        else:
            if isinstance(last_page, bool) or not isinstance(last_page, int):
                raise TypeError("last_page must be an integer.")
            if last_page < 1:
                raise ValueError("last_page must be >= 1.")
            self.last_page = last_page

    @property
    def has_more_pages(self) -> bool:
        """Return True if there are pages beyond the current page."""
        return self.current_page < self.last_page

    @property
    def has_previous_page(self) -> bool:
        """Return True if the current page is greater than page 1."""
        return self.current_page > 1

    @property
    def first_item(self) -> int | None:
        """Return the 1-based index of the first record in the current page, or None if empty."""
        if not self.items:
            return None
        return (self.current_page - 1) * self.per_page + 1

    @property
    def last_item(self) -> int | None:
        """Return the 1-based index of the last record in the current page, or None if empty."""
        first = self.first_item
        if first is None:
            return None
        return first + len(self.items) - 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize paginator data and metadata into a standard dictionary payload."""
        serialized_items: list[Any] = []
        for item in self.items:
            if hasattr(item, "to_dict") and callable(item.to_dict):
                serialized_items.append(item.to_dict())
            elif isinstance(item, dict):
                serialized_items.append(dict(item))
            else:
                serialized_items.append(item)

        return {
            "data": serialized_items,
            "current_page": self.current_page,
            "per_page": self.per_page,
            "total": self.total,
            "last_page": self.last_page,
            "from": self.first_item,
            "to": self.last_item,
            "has_more_pages": self.has_more_pages,
            "has_previous_page": self.has_previous_page,
        }

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> T:
        return self.items[index]

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} page={self.current_page}/{self.last_page} "
            f"count={len(self.items)} total={self.total}>"
        )
