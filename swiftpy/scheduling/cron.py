from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from swiftpy.scheduling.exceptions import (
    InvalidCronError,
    InvalidScheduleError,
    InvalidTimezoneError,
)

_CRON_MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTH_NAMES: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DOW_NAMES: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

_MAX_DAYS_BY_MONTH: dict[int, int] = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}

_MAX_SEARCH_YEARS: int = 10


def resolve_timezone(tz: str | tzinfo | None = None) -> tuple[tzinfo, str]:
    """
    Validate and resolve a timezone specifier into ``(tzinfo, canonical_name)``.

    Never silently falls back to the host OS local timezone; ``None`` defaults to ``"UTC"``.
    """
    if tz is None:
        return UTC, "UTC"

    if isinstance(tz, ZoneInfo):
        return tz, tz.key

    if isinstance(tz, tzinfo):
        name = tz.tzname(None) or repr(tz)
        return tz, name

    if not isinstance(tz, str) or not tz.strip():
        raise InvalidTimezoneError(f"Timezone must be a non-empty string or tzinfo, got {tz!r}")

    cleaned = tz.strip()
    if cleaned.upper() == "UTC":
        return UTC, "UTC"

    try:
        zone = ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise InvalidTimezoneError(f"Unknown IANA timezone '{cleaned}'") from exc
    return zone, zone.key


def normalize_schedule_datetime(
    dt: datetime,
    default_timezone: str | tzinfo | None = "UTC",
) -> tuple[datetime, str]:
    """
    Normalize an explicit scheduled ``datetime`` into UTC-aware form plus timezone label.

    - Timezone-aware datetimes convert directly to UTC while preserving their timezone name.
    - Naive datetimes are attached strictly in ``default_timezone`` (default ``"UTC"``),
      rejecting nonexistent spring-forward DST wall times with ``InvalidScheduleError``.
    """
    if not isinstance(dt, datetime):
        raise InvalidScheduleError(
            f"Scheduled datetime must be a datetime instance, got {type(dt).__name__}"
        )

    tz_obj, tz_name = resolve_timezone(default_timezone)
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        resolved_name = (
            dt.tzinfo.key
            if isinstance(dt.tzinfo, ZoneInfo)
            else (dt.tzname() or tz_name)
        )
        return dt.astimezone(UTC), resolved_name

    # Attach naive wall-clock time strictly using target tzinfo and verify existence.
    localized = dt.replace(tzinfo=tz_obj, fold=0)
    as_utc = localized.astimezone(UTC)
    round_trip = as_utc.astimezone(tz_obj).replace(tzinfo=None)
    if round_trip != dt:
        raise InvalidScheduleError(
            f"Local datetime {dt.isoformat(sep=' ')} does not exist in timezone '{tz_name}' "
            f"(skipped by daylight-saving transition)."
        )
    return as_utc, tz_name


def _parse_cron_atom(
    token: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None,
    *,
    is_dow: bool,
    as_range_end: bool = False,
) -> int:
    lower = token.strip().lower()
    if not lower:
        raise InvalidCronError("Empty token in cron expression field.")

    if names is not None and lower in names:
        mapped = names[lower]
        if is_dow and as_range_end and lower == "sun" and mapped == 0:
            return 7
        return mapped

    for modifier in ("l", "w"):
        if modifier in lower:
            raise InvalidCronError(
                f"Unsupported cron modifier '{modifier.upper()}' in token '{token}'. "
                "Only standard 5-field Vixie cron expressions are supported."
            )

    if not lower.isdigit():
        raise InvalidCronError(f"Invalid cron value '{token}'.")

    val = int(lower)
    allowed_max = 7 if is_dow else max_val
    if val < min_val or val > allowed_max:
        raise InvalidCronError(
            f"Cron value {val} out of range [{min_val}, {allowed_max}]."
        )
    if is_dow and val == 7 and not as_range_end:
        return 0
    return val


def _parse_cron_field(
    field_expr: str,
    *,
    field_name: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None = None,
    is_dow: bool = False,
) -> tuple[frozenset[int], bool]:
    if not field_expr:
        raise InvalidCronError(f"Empty cron {field_name} field.")

    for symbol in ("#", "?"):
        if symbol in field_expr:
            raise InvalidCronError(
                f"Unsupported cron modifier '{symbol}' in {field_name} field '{field_expr}'. "
                "Only standard 5-field Vixie cron expressions are supported."
            )

    if field_expr == "*":
        full = frozenset(range(min_val, max_val + 1))
        return full, True

    values: set[int] = set()
    segments = field_expr.split(",")
    for segment in segments:
        if not segment:
            raise InvalidCronError(
                f"Malformed comma list in cron {field_name} field '{field_expr}'."
            )

        slash_parts = segment.split("/")
        if len(slash_parts) > 2:
            raise InvalidCronError(
                f"Invalid step syntax '{segment}' in cron {field_name} field."
            )

        base = slash_parts[0]
        if len(slash_parts) == 2:
            step_str = slash_parts[1]
            if not step_str.isdigit():
                raise InvalidCronError(
                    f"Cron step must be a positive integer in '{segment}'."
                )
            step = int(step_str)
            if step <= 0:
                raise InvalidCronError(f"Cron step must be >= 1 in '{segment}'.")
            max_span = (max_val - min_val) + 1
            if step > max_span:
                raise InvalidCronError(
                    f"Cron step {step} exceeds valid range span ({max_span}) in {field_name} field."
                )
        else:
            step = 1

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2 or not bounds[0] or not bounds[1]:
                raise InvalidCronError(
                    f"Invalid range syntax '{base}' in cron {field_name} field."
                )
            start = _parse_cron_atom(
                bounds[0], min_val, max_val, names, is_dow=is_dow, as_range_end=False
            )
            end = _parse_cron_atom(
                bounds[1], min_val, max_val, names, is_dow=is_dow, as_range_end=True
            )
            if start > end:
                raise InvalidCronError(
                    f"Inverted range '{base}' ({start} > {end}) in cron {field_name} field."
                )
        else:
            single = _parse_cron_atom(
                base, min_val, max_val, names, is_dow=is_dow, as_range_end=False
            )
            if len(slash_parts) == 2:
                start, end = single, max_val
            else:
                values.add(0 if (is_dow and single == 7) else single)
                continue

        for candidate in range(start, end + 1, step):
            values.add(0 if (is_dow and candidate == 7) else candidate)

    if not values:
        raise InvalidCronError(f"Cron {field_name} field '{field_expr}' matches no values.")

    return frozenset(values), False


@dataclass(frozen=True, slots=True)
class CronExpression:
    """
    Validated 5-field Vixie cron expression with IANA timezone & DST-safe
    next-occurrence calculation.
    """

    raw: str
    normalized: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days_of_month: frozenset[int]
    months: tuple[int, ...]
    days_of_week: frozenset[int]
    dom_is_wildcard: bool
    dow_is_wildcard: bool
    hour_is_wildcard: bool

    def _matches_date(self, candidate_date: date) -> bool:
        if candidate_date.month not in self.months:
            return False

        dom_match = candidate_date.day in self.days_of_month
        # Python weekday(): Mon=0..Sun=6 -> cron dow: Sun=0, Mon=1..Sat=6
        cron_dow = (candidate_date.weekday() + 1) % 7
        dow_match = cron_dow in self.days_of_week

        if not self.dom_is_wildcard and not self.dow_is_wildcard:
            return dom_match or dow_match
        return dom_match and dow_match

    def matches_local(self, local_dt: datetime) -> bool:
        """Return True if a localized datetime matches every cron field."""
        if local_dt.month not in self.months:
            return False
        if local_dt.hour not in self.hours:
            return False
        if local_dt.minute not in self.minutes:
            return False
        return self._matches_date(local_dt.date())

    def next_run_after(
        self,
        after: datetime,
        tz: str | tzinfo | None = "UTC",
        *,
        inclusive: bool = False,
    ) -> datetime:
        """
        Compute the next UTC occurrence matching this cron expression in ``tz``.

        DST semantics:
        - Nonexistent spring-forward local wall times (``02:30`` when clocks skip
          ``02:00 -> 03:00``) are skipped cleanly without raising or firing early.
        - On fall-back repeated wall hours, wildcard-hour schedules (e.g. ``*``,
          ``*/2``, ``0 * * * *``) continue firing on real UTC cadence (``fold in (0, 1)``),
          while fixed-hour schedules (e.g. ``30 1 * * *``) fire strictly once (``fold=0``).
        """
        if not isinstance(after, datetime):
            raise InvalidScheduleError(
                f"'after' must be a datetime instance, got {type(after).__name__}"
            )

        tz_obj, tz_name = resolve_timezone(tz)
        after_utc = (
            after.replace(tzinfo=tz_obj, fold=0).astimezone(UTC)
            if after.tzinfo is None
            else after.astimezone(UTC)
        )
        local_ref = after_utc.astimezone(tz_obj)
        # Start one local day back to avoid edge misses across midnight/negative offsets.
        cursor_date = local_ref.date() - timedelta(days=1)
        end_date = date(cursor_date.year + _MAX_SEARCH_YEARS, 12, 31)

        month_set = set(self.months)
        folds: tuple[int, ...] = (0, 1) if self.hour_is_wildcard else (0,)

        while cursor_date <= end_date:
            if cursor_date.month not in month_set:
                next_months = [m for m in self.months if m > cursor_date.month]
                if next_months:
                    cursor_date = date(cursor_date.year, next_months[0], 1)
                else:
                    cursor_date = date(cursor_date.year + 1, self.months[0], 1)
                continue

            if self._matches_date(cursor_date):
                day_candidates: list[datetime] = []
                for hour in self.hours:
                    for minute in self.minutes:
                        seen_utc: set[datetime] = set()
                        for fold_val in folds:
                            localized = datetime(
                                cursor_date.year,
                                cursor_date.month,
                                cursor_date.day,
                                hour,
                                minute,
                                tzinfo=tz_obj,
                                fold=fold_val,
                            )
                            cand_utc = localized.astimezone(UTC)
                            if cand_utc in seen_utc:
                                continue
                            seen_utc.add(cand_utc)
                            local_rt = cand_utc.astimezone(tz_obj)
                            if (
                                local_rt.year,
                                local_rt.month,
                                local_rt.day,
                                local_rt.hour,
                                local_rt.minute,
                            ) != (
                                cursor_date.year,
                                cursor_date.month,
                                cursor_date.day,
                                hour,
                                minute,
                            ):
                                continue
                            if inclusive:
                                if cand_utc >= after_utc:
                                    day_candidates.append(cand_utc)
                            elif cand_utc > after_utc:
                                day_candidates.append(cand_utc)

                if day_candidates:
                    day_candidates.sort()
                    return day_candidates[0]

            cursor_date += timedelta(days=1)

        raise InvalidCronError(
            f"Cron expression '{self.raw}' produces no matching occurrence within "
            f"{_MAX_SEARCH_YEARS} years in timezone '{tz_name}'."
        )


@functools.lru_cache(maxsize=256)
def parse_cron(expression: str) -> CronExpression:
    """Validate and compile a standard 5-field Vixie cron expression or macro."""
    if not isinstance(expression, str):
        raise InvalidCronError(
            f"Cron expression must be a string, got {type(expression).__name__}"
        )

    cleaned = expression.strip()
    if not cleaned:
        raise InvalidCronError("Cron expression cannot be empty.")

    lower_cleaned = cleaned.lower()
    if lower_cleaned.startswith("@"):
        if lower_cleaned not in _CRON_MACROS:
            raise InvalidCronError(f"Unsupported cron macro '{cleaned}'.")
        expanded = _CRON_MACROS[lower_cleaned]
    else:
        expanded = cleaned

    parts = expanded.split()
    if len(parts) != 5:
        raise InvalidCronError(
            f"Invalid cron expression '{expression}': expected 5 fields "
            f"(minute hour dom month dow), got {len(parts)}."
        )

    min_f, hour_f, dom_f, mon_f, dow_f = parts
    minutes_set, _ = _parse_cron_field(
        min_f, field_name="minute", min_val=0, max_val=59
    )
    hours_set, hour_wild = _parse_cron_field(
        hour_f, field_name="hour", min_val=0, max_val=23
    )
    hour_is_wildcard = hour_wild or hour_f.startswith("*/")
    dom_set, dom_wild = _parse_cron_field(
        dom_f, field_name="day_of_month", min_val=1, max_val=31
    )
    months_set, _ = _parse_cron_field(
        mon_f, field_name="month", min_val=1, max_val=12, names=_MONTH_NAMES
    )
    dow_set, dow_wild = _parse_cron_field(
        dow_f, field_name="day_of_week", min_val=0, max_val=6, names=_DOW_NAMES, is_dow=True
    )

    if not dom_wild and dow_wild:
        possible = any(
            day <= _MAX_DAYS_BY_MONTH[month]
            for month in months_set
            for day in dom_set
        )
        if not possible:
            raise InvalidCronError(
                f"Impossible calendar date in cron expression '{expression}'."
            )

    return CronExpression(
        raw=expression,
        normalized=expanded,
        minutes=tuple(sorted(minutes_set)),
        hours=tuple(sorted(hours_set)),
        days_of_month=dom_set,
        months=tuple(sorted(months_set)),
        days_of_week=dow_set,
        dom_is_wildcard=dom_wild,
        dow_is_wildcard=dow_wild,
        hour_is_wildcard=hour_is_wildcard,
    )


__all__ = [
    "CronExpression",
    "normalize_schedule_datetime",
    "parse_cron",
    "resolve_timezone",
]
