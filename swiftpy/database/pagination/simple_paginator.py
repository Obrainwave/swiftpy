from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class SimplePaginator[T]:
    """
    Lightweight paginator that skips expensive COUNT(*) operations by querying
    for an extra record (per_page + 1) to determine if additional pages exist.
    """

    def __init__(
        self,
        items: list[T],
        per_page: int,
        current_page: int,
        has_more: bool,
    ) -> None:
        if isinstance(per_page, bool) or not isinstance(per_page, int):
            raise TypeError("per_page must be an integer.")
        if per_page < 1:
            raise ValueError("per_page must be >= 1.")

        if isinstance(current_page, bool) or not isinstance(current_page, int):
            raise TypeError("current_page must be an integer.")
        if current_page < 1:
            raise ValueError("current_page must be >= 1.")

        if not isinstance(has_more, bool):
            raise TypeError("has_more must be a boolean.")

        self.items: list[T] = items
        self.per_page: int = per_page
        self.current_page: int = current_page
        self._has_more: bool = has_more

    @property
    def has_more_pages(self) -> bool:
        """Return True if additional pages exist beyond the current page."""
        return self._has_more

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
        """Serialize simple paginator data and metadata into a standard dictionary payload."""
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
            f"<{self.__class__.__name__} page={self.current_page} "
            f"count={len(self.items)} has_more={self._has_more}>"
        )
