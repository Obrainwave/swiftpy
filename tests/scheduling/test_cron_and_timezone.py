from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from swiftpy.scheduling import (
    InvalidCronError,
    InvalidScheduleError,
    InvalidTimezoneError,
    MisfirePolicy,
    OccurrenceStatus,
    OverlapPolicy,
    ScheduleOccurrence,
    ScheduleRecord,
    TriggerType,
    normalize_schedule_datetime,
    parse_cron,
    resolve_timezone,
)


def test_parse_cron_macros_ranges_steps_and_named_aliases() -> None:
    expr = parse_cron("*/15 9-17 1,15 jan-mar,jul mon-fri")
    assert expr.minutes == (0, 15, 30, 45)
    assert expr.hours == tuple(range(9, 18))
    assert expr.days_of_month == frozenset({1, 15})
    assert expr.months == (1, 2, 3, 7)
    assert expr.days_of_week == frozenset({1, 2, 3, 4, 5})

    # Sunday aliases: 0, 7, sun, mon-sun, sun-tue
    assert parse_cron("0 0 * * 7").days_of_week == frozenset({0})
    assert parse_cron("0 0 * * sun").days_of_week == frozenset({0})
    assert parse_cron("0 0 * * mon-sun").days_of_week == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * sun-tue").days_of_week == frozenset({0, 1, 2})

    # Macros
    assert parse_cron("@hourly").normalized == "0 * * * *"
    assert parse_cron("@daily").normalized == "0 0 * * *"
    assert parse_cron("@weekly").normalized == "0 0 * * 0"
    assert parse_cron("@monthly").normalized == "0 0 1 * *"
    assert parse_cron("@yearly").normalized == "0 0 1 1 *"

    # Wedge day name with 'w' and month with 'l' must NOT trigger false modifier rejection
    wed_jul = parse_cron("30 10 * jul wed")
    assert wed_jul.months == (7,)
    assert wed_jul.days_of_week == frozenset({3})

    # Step with base offset (5/15 -> 5, 20, 35, 50)
    assert parse_cron("5/15 * * * *").minutes == (5, 20, 35, 50)


def test_parse_cron_rejects_invalid_syntax_extensions_and_impossible_dates() -> None:
    invalid_expressions = [
        "",
        "   ",
        "* * * *",  # 4 fields
        "0 * * * * *",  # 6 fields unsupported
        "@reboot",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "*/61 * * * *",
        "5-2 * * * *",
        "fri-mon * * * *",
        "1,,2 * * * *",
        "1-5/2/3 * * * *",
        "0 0 L * *",
        "0 0 15W * *",
        "0 0 * * 1#1",
        "0 0 ? * 1",
        "0 0 30 2 *",  # Feb 30 impossible
        "0 0 31 2 *",  # Feb 31 impossible
        "0 0 31 4,6,9,11 *",  # April/June/Sept/Nov 31 impossible
    ]
    for bad in invalid_expressions:
        with pytest.raises(InvalidCronError):
            parse_cron(bad)


def test_vixie_dom_dow_or_semantics_and_leap_day() -> None:
    # Both DOM and DOW restricted -> matches EITHER 15th OR Friday
    expr = parse_cron("0 12 15 * fri")
    # 2026-01-02 is Friday (not 15th) -> matches!
    nxt = expr.next_run_after(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), "UTC")
    assert nxt == datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    # After 2026-01-09 (Fri) comes Thu 2026-01-15 (15th)
    nxt2 = expr.next_run_after(datetime(2026, 1, 9, 12, 0, tzinfo=UTC), "UTC")
    assert nxt2 == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    # Leap-year day Feb 29 (including 2096 -> 2104 8-year leap skip across 2100)
    leap_expr = parse_cron("0 0 29 feb *")
    assert leap_expr.next_run_after(
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC), "UTC"
    ) == datetime(2028, 2, 29, 0, 0, tzinfo=UTC)
    assert leap_expr.next_run_after(
        datetime(2096, 3, 1, 0, 0, tzinfo=UTC), "UTC"
    ) == datetime(2104, 2, 29, 0, 0, tzinfo=UTC)


def test_timezone_resolution_and_dst_transitions() -> None:
    tz, name = resolve_timezone("America/New_York")
    assert isinstance(tz, ZoneInfo)
    assert name == "America/New_York"
    assert resolve_timezone(None) == (UTC, "UTC")

    with pytest.raises(InvalidTimezoneError):
        resolve_timezone("Mars/Olympus_Mons")
    with pytest.raises(InvalidTimezoneError):
        resolve_timezone("")

    # Naive datetime attaches configured target timezone; nonexistent spring-forward time rejected
    naive_valid_utc, naive_valid_tz = normalize_schedule_datetime(
        datetime.fromisoformat("2026-06-01T09:00:00"), "America/New_York"
    )
    assert naive_valid_utc == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    assert naive_valid_tz == "America/New_York"

    # Spring-forward in America/New_York: 2026-03-08 02:00 -> 03:00 (02:30 nonexistent)
    with pytest.raises(InvalidScheduleError):
        normalize_schedule_datetime(
            datetime.fromisoformat("2026-03-08T02:30:00"), "America/New_York"
        )

    # Aware datetime converts accurately to UTC preserving named zone
    fixed = timezone(timedelta(hours=2))
    utc_dt, resolved = normalize_schedule_datetime(
        datetime(2026, 5, 1, 12, 0, tzinfo=fixed)
    )
    assert utc_dt == datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    assert resolved

    # Daily cron at 02:30 skips nonexistent 2026-03-08 cleanly -> fires 2026-03-09 02:30 EDT (06:30 UTC)
    spring_cron = parse_cron("30 2 * * *")
    before_spring = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)  # 00:00 EST on Mar 8
    next_spring = spring_cron.next_run_after(before_spring, "America/New_York")
    assert next_spring == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    assert next_spring.astimezone(ZoneInfo("America/New_York")).hour == 2
    assert next_spring.astimezone(ZoneInfo("America/New_York")).minute == 30

    # Fall-back in America/New_York: 2026-11-01 02:00 EDT -> 01:00 EST (01:30 happens twice)
    # Fixed-hour daily 01:30 cron must fire ONCE at fold=0 (05:30 UTC), NOT again at 06:30 UTC
    fall_cron = parse_cron("30 1 * * *")
    first_fall = fall_cron.next_run_after(
        datetime(2026, 10, 31, 6, 0, tzinfo=UTC), "America/New_York"
    )
    assert first_fall == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    after_first_fall = fall_cron.next_run_after(first_fall, "America/New_York")
    assert after_first_fall == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)

    # Hourly cron fires every 60 real UTC minutes across fall-back without skipping an hour
    hourly_cron = parse_cron("0 * * * *")
    t1 = hourly_cron.next_run_after(
        datetime(2026, 11, 1, 4, 30, tzinfo=UTC), "America/New_York"
    )
    t2 = hourly_cron.next_run_after(t1, "America/New_York")
    t3 = hourly_cron.next_run_after(t2, "America/New_York")
    assert (t1, t2, t3) == (
        datetime(2026, 11, 1, 5, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
    )


def test_schedule_record_and_occurrence_roundtrip_serialization() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    rec = ScheduleRecord(
        id="sched-1",
        name="daily-digest",
        trigger=TriggerType.CRON,
        expression="0 9 * * *",
        timezone="Europe/Berlin",
        task_name="app.jobs:SendDigestJob",
        kwargs={"batch": 10},
        overlap_policy=OverlapPolicy.SKIP,
        misfire_policy=MisfirePolicy.FIRE_ONCE,
        lock_ttl=900,
        next_run_at=now + timedelta(hours=1),
        last_status=OccurrenceStatus.COMPLETED,
    )
    restored_rec = ScheduleRecord.from_dict(rec.to_dict())
    assert restored_rec == rec

    occ = ScheduleOccurrence(
        occurrence_id="sched-1:1780318800000",
        schedule_id="sched-1",
        scheduled_for=now + timedelta(hours=1),
        status=OccurrenceStatus.DISPATCH_PENDING,
        task_name="app.jobs:SendDigestJob",
        queue="default",
        lease_until=now.timestamp() + 30.0,
        dispatch_attempts=1,
        lock_key="swiftpy_schedule:overlap:sched-1",
        lock_token="tok-123",
        lock_ttl=900,
    )
    restored_occ = ScheduleOccurrence.from_dict(occ.to_dict())
    assert restored_occ == occ
