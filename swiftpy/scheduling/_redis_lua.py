from __future__ import annotations

import json
from collections.abc import Mapping

from swiftpy.scheduling.exceptions import InvalidScheduleError
from swiftpy.scheduling.models import (
    ScheduleOccurrence,
    ScheduleRecord,
    format_utc_datetime,
)

NULLABLE_SCHEDULE_FIELDS: tuple[str, ...] = (
    "name",
    "expression",
    "interval_seconds",
    "run_at",
    "lock_ttl",
    "grace_seconds",
    "next_run_at",
    "next_run_ts",
    "last_run_at",
    "last_claimed_occurrence",
    "last_occurrence_id",
    "last_status",
    "last_error",
)


def flat_list_to_dict(raw: object) -> dict[str, str]:
    """Normalize a Redis ``HGETALL`` response (dict or alternating list) into ``dict[str, str]``."""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for idx in range(0, len(raw) - 1, 2):
            result[str(raw[idx])] = str(raw[idx + 1])
        return result
    return {}


def schedule_to_lua_payload(record: ScheduleRecord) -> dict[str, str]:
    """Encode a ``ScheduleRecord`` into a flat string-only dict safe for Redis Hashes & Lua JSON."""
    created_at_iso = format_utc_datetime(record.created_at)
    updated_at_iso = format_utc_datetime(record.updated_at)
    assert created_at_iso is not None
    assert updated_at_iso is not None

    next_run_iso = format_utc_datetime(record.next_run_at)
    return {
        "id": record.id,
        "name": record.name if record.name is not None else "",
        "trigger": record.trigger.value,
        "expression": record.expression if record.expression is not None else "",
        "interval_seconds": (
            repr(float(record.interval_seconds))
            if record.interval_seconds is not None
            else ""
        ),
        "run_at": format_utc_datetime(record.run_at) or "",
        "timezone": record.timezone,
        "task_name": record.task_name,
        "task_kind": record.task_kind,
        "args_json": json.dumps(list(record.args), ensure_ascii=False, allow_nan=False),
        "kwargs_json": json.dumps(
            dict(record.kwargs), ensure_ascii=False, allow_nan=False
        ),
        "queue": record.queue,
        "enabled": "1" if record.enabled else "0",
        "overlap_policy": record.overlap_policy.value,
        "misfire_policy": record.misfire_policy.value,
        "lock_ttl": str(int(record.lock_ttl)) if record.lock_ttl is not None else "",
        "grace_seconds": (
            repr(float(record.grace_seconds))
            if record.grace_seconds is not None
            else ""
        ),
        "next_run_at": next_run_iso or "",
        "next_run_ts": (
            repr(record.next_run_at.timestamp())
            if record.next_run_at is not None
            else ""
        ),
        "last_run_at": format_utc_datetime(record.last_run_at) or "",
        "last_claimed_occurrence": record.last_claimed_occurrence or "",
        "last_occurrence_id": record.last_occurrence_id or "",
        "last_status": (
            record.last_status.value if record.last_status is not None else ""
        ),
        "last_error": record.last_error if record.last_error is not None else "",
        "has_last_error": "1" if record.last_error is not None else "0",
        "created_at": created_at_iso,
        "updated_at": updated_at_iso,
        "signature": record.signature or "",
    }


def hash_to_schedule(raw_hash: Mapping[str, str]) -> ScheduleRecord:
    """Deserialize a Redis schedule hash mapping into a ``ScheduleRecord``."""
    try:
        raw_args = json.loads(raw_hash.get("args_json", "[]"))
        raw_kwargs = json.loads(raw_hash.get("kwargs_json", "{}"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidScheduleError(f"Corrupt serialized schedule arguments: {exc}") from exc

    data: dict[str, object] = {
        "id": raw_hash["id"],
        "name": raw_hash.get("name"),
        "trigger": raw_hash["trigger"],
        "expression": raw_hash.get("expression"),
        "interval_seconds": raw_hash.get("interval_seconds"),
        "run_at": raw_hash.get("run_at"),
        "timezone": raw_hash.get("timezone", "UTC"),
        "task_name": raw_hash["task_name"],
        "task_kind": raw_hash.get("task_kind", "job"),
        "args": raw_args if isinstance(raw_args, list) else [],
        "kwargs": raw_kwargs if isinstance(raw_kwargs, dict) else {},
        "queue": raw_hash.get("queue", "default"),
        "enabled": raw_hash.get("enabled", "1") == "1",
        "overlap_policy": raw_hash.get("overlap_policy", "allow"),
        "misfire_policy": raw_hash.get("misfire_policy", "skip"),
        "lock_ttl": raw_hash.get("lock_ttl"),
        "grace_seconds": raw_hash.get("grace_seconds"),
        "next_run_at": raw_hash.get("next_run_at"),
        "last_run_at": raw_hash.get("last_run_at"),
        "last_claimed_occurrence": raw_hash.get("last_claimed_occurrence"),
        "last_occurrence_id": raw_hash.get("last_occurrence_id"),
        "last_status": raw_hash.get("last_status"),
        "last_error": raw_hash.get("last_error"),
        "created_at": raw_hash.get("created_at"),
        "updated_at": raw_hash.get("updated_at"),
        "signature": raw_hash.get("signature"),
    }
    return ScheduleRecord.from_dict(data)


def hash_to_occurrence(raw_hash: Mapping[str, str]) -> ScheduleOccurrence:
    """Deserialize a Redis occurrence hash mapping into a ``ScheduleOccurrence``."""
    try:
        raw_args = json.loads(raw_hash.get("args_json", "[]"))
        raw_kwargs = json.loads(raw_hash.get("kwargs_json", "{}"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidScheduleError(f"Corrupt serialized occurrence arguments: {exc}") from exc

    data: dict[str, object] = {
        "occurrence_id": raw_hash["occurrence_id"],
        "schedule_id": raw_hash["schedule_id"],
        "scheduled_for": raw_hash.get("scheduled_for"),
        "status": raw_hash["status"],
        "task_name": raw_hash["task_name"],
        "task_kind": raw_hash.get("task_kind", "job"),
        "args": raw_args if isinstance(raw_args, list) else [],
        "kwargs": raw_kwargs if isinstance(raw_kwargs, dict) else {},
        "queue": raw_hash.get("queue", "default"),
        "job_id": raw_hash.get("job_id"),
        "claimed_by": raw_hash.get("claimed_by"),
        "claimed_at": raw_hash.get("claimed_at"),
        "dispatched_at": raw_hash.get("dispatched_at"),
        "started_at": raw_hash.get("started_at"),
        "finished_at": raw_hash.get("finished_at"),
        "lease_until": raw_hash.get("lease_until"),
        "dispatch_attempts": raw_hash.get("dispatch_attempts", "0"),
        "lock_key": raw_hash.get("lock_key"),
        "lock_token": raw_hash.get("lock_token"),
        "lock_ttl": raw_hash.get("lock_ttl"),
        "error": raw_hash.get("error"),
        "signature": raw_hash.get("signature"),
    }
    return ScheduleOccurrence.from_dict(data)


_LUA_HELPERS = """
local function release_lock(lock_key, lock_token)
    if lock_key and lock_key ~= '' and lock_token and lock_token ~= '' then
        if redis.call('GET', lock_key) == lock_token then
            redis.call('DEL', lock_key)
            return 1
        end
    end
    return 0
end

local function set_optional_field(hash_key, field, value)
    if value and value ~= '' then
        redis.call('HSET', hash_key, field, value)
    else
        redis.call('HDEL', hash_key, field)
    end
end

local function sync_due_index(due_key, sched_id, enabled_val, next_run_ts)
    if enabled_val == '1' and next_run_ts and next_run_ts ~= '' then
        redis.call('ZADD', due_key, tonumber(next_run_ts), sched_id)
    else
        redis.call('ZREM', due_key, sched_id)
    end
end
"""

SAVE_SCHEDULE_LUA = (
    _LUA_HELPERS
    + """
local schedules_key = KEYS[1]
local names_key = KEYS[2]
local due_key = KEYS[3]

local prefix = ARGV[1]
local upsert = ARGV[2] == '1'
local payload = cjson.decode(ARGV[3])

local rec_id = payload['id']
local rec_name = payload['name'] or ''

local existing_id = nil
if redis.call('EXISTS', prefix .. 'schedule:' .. rec_id) == 1 then
    existing_id = rec_id
elseif rec_name ~= '' then
    local mapped_id = redis.call('HGET', names_key, rec_name)
    if mapped_id then
        if redis.call('EXISTS', prefix .. 'schedule:' .. mapped_id) == 1 then
            existing_id = mapped_id
        else
            redis.call('HDEL', names_key, rec_name)
        end
    end
end

if existing_id then
    if not upsert then
        local label = rec_name ~= '' and rec_name or rec_id
        return {'err', 'duplicate', label}
    end

    if rec_name ~= '' then
        local name_owner = redis.call('HGET', names_key, rec_name)
        if name_owner and name_owner ~= existing_id then
            if redis.call('EXISTS', prefix .. 'schedule:' .. name_owner) == 1 then
                return {'err', 'duplicate', rec_name}
            end
        end
    end

    local sched_key = prefix .. 'schedule:' .. existing_id
    local old_name = redis.call('HGET', sched_key, 'name') or ''
    local cur_trigger = redis.call('HGET', sched_key, 'trigger') or ''
    local cur_expr = redis.call('HGET', sched_key, 'expression') or ''
    local cur_interval = redis.call('HGET', sched_key, 'interval_seconds') or ''
    local cur_run_at = redis.call('HGET', sched_key, 'run_at') or ''
    local cur_tz = redis.call('HGET', sched_key, 'timezone') or ''
    local existing_enabled = redis.call('HGET', sched_key, 'enabled') or '1'

    local timing_changed = (
        cur_trigger ~= (payload['trigger'] or '')
        or cur_expr ~= (payload['expression'] or '')
        or cur_interval ~= (payload['interval_seconds'] or '')
        or cur_run_at ~= (payload['run_at'] or '')
        or cur_tz ~= (payload['timezone'] or '')
    )

    if old_name ~= '' and old_name ~= rec_name then
        redis.call('HDEL', names_key, old_name)
    end
    if rec_name ~= '' then
        redis.call('HSET', names_key, rec_name, existing_id)
    end

    redis.call('SADD', schedules_key, existing_id)
    redis.call('HSET', sched_key,
        'id', existing_id,
        'trigger', payload['trigger'],
        'timezone', payload['timezone'],
        'task_name', payload['task_name'],
        'task_kind', payload['task_kind'],
        'args_json', payload['args_json'],
        'kwargs_json', payload['kwargs_json'],
        'queue', payload['queue'],
        'overlap_policy', payload['overlap_policy'],
        'misfire_policy', payload['misfire_policy'],
        'updated_at', payload['updated_at']
    )
    set_optional_field(sched_key, 'name', rec_name)
    set_optional_field(sched_key, 'expression', payload['expression'])
    set_optional_field(sched_key, 'interval_seconds', payload['interval_seconds'])
    set_optional_field(sched_key, 'run_at', payload['run_at'])
    set_optional_field(sched_key, 'lock_ttl', payload['lock_ttl'])
    set_optional_field(sched_key, 'signature', payload['signature'])
    set_optional_field(sched_key, 'grace_seconds', payload['grace_seconds'])

    if timing_changed then
        set_optional_field(sched_key, 'next_run_at', payload['next_run_at'])
        set_optional_field(sched_key, 'next_run_ts', payload['next_run_ts'])
        sync_due_index(due_key, existing_id, existing_enabled, payload['next_run_ts'])
    end

    return {'upserted', existing_id, redis.call('HGETALL', sched_key)}
end

redis.call('SADD', schedules_key, rec_id)
if rec_name ~= '' then
    redis.call('HSET', names_key, rec_name, rec_id)
end

local new_key = prefix .. 'schedule:' .. rec_id
redis.call('HSET', new_key,
    'id', rec_id,
    'trigger', payload['trigger'],
    'timezone', payload['timezone'],
    'task_name', payload['task_name'],
    'task_kind', payload['task_kind'],
    'args_json', payload['args_json'],
    'kwargs_json', payload['kwargs_json'],
    'queue', payload['queue'],
    'enabled', payload['enabled'],
    'overlap_policy', payload['overlap_policy'],
    'misfire_policy', payload['misfire_policy'],
    'created_at', payload['created_at'],
    'updated_at', payload['updated_at']
)
set_optional_field(new_key, 'name', rec_name)
set_optional_field(new_key, 'expression', payload['expression'])
set_optional_field(new_key, 'interval_seconds', payload['interval_seconds'])
set_optional_field(new_key, 'run_at', payload['run_at'])
set_optional_field(new_key, 'lock_ttl', payload['lock_ttl'])
set_optional_field(new_key, 'signature', payload['signature'])
set_optional_field(new_key, 'grace_seconds', payload['grace_seconds'])
set_optional_field(new_key, 'next_run_at', payload['next_run_at'])
set_optional_field(new_key, 'next_run_ts', payload['next_run_ts'])
set_optional_field(new_key, 'last_run_at', payload['last_run_at'])
set_optional_field(new_key, 'last_claimed_occurrence', payload['last_claimed_occurrence'])
set_optional_field(new_key, 'last_occurrence_id', payload['last_occurrence_id'])
set_optional_field(new_key, 'last_status', payload['last_status'])
if payload['has_last_error'] == '1' then
    redis.call('HSET', new_key, 'last_error', payload['last_error'])
else
    redis.call('HDEL', new_key, 'last_error')
end

sync_due_index(due_key, rec_id, payload['enabled'], payload['next_run_ts'])
return {'created', rec_id, redis.call('HGETALL', new_key)}
"""
)

GET_SCHEDULE_LUA = """
local names_key = KEYS[1]
local prefix = ARGV[1]
local lookup = ARGV[2]

local direct_key = prefix .. 'schedule:' .. lookup
if redis.call('EXISTS', direct_key) == 1 then
    return redis.call('HGETALL', direct_key)
end

local mapped_id = redis.call('HGET', names_key, lookup)
if not mapped_id then
    return {}
end

local mapped_key = prefix .. 'schedule:' .. mapped_id
if redis.call('EXISTS', mapped_key) == 1 then
    return redis.call('HGETALL', mapped_key)
end

redis.call('HDEL', names_key, lookup)
return {}
"""

UPDATE_SCHEDULE_LUA = (
    _LUA_HELPERS
    + """
local names_key = KEYS[1]
local due_key = KEYS[2]
local prefix = ARGV[1]
local payload = cjson.decode(ARGV[2])

local rec_id = payload['id']
local sched_key = prefix .. 'schedule:' .. rec_id
if redis.call('EXISTS', sched_key) == 0 then
    return {'err', 'not_found', rec_id}
end

local rec_name = payload['name'] or ''
if rec_name ~= '' then
    local name_owner = redis.call('HGET', names_key, rec_name)
    if name_owner and name_owner ~= rec_id then
        if redis.call('EXISTS', prefix .. 'schedule:' .. name_owner) == 1 then
            return {'err', 'duplicate', rec_name}
        end
    end
end

local old_name = redis.call('HGET', sched_key, 'name') or ''
if old_name ~= '' and old_name ~= rec_name then
    redis.call('HDEL', names_key, old_name)
end
if rec_name ~= '' then
    redis.call('HSET', names_key, rec_name, rec_id)
end

redis.call('HSET', sched_key,
    'id', rec_id,
    'trigger', payload['trigger'],
    'timezone', payload['timezone'],
    'task_name', payload['task_name'],
    'task_kind', payload['task_kind'],
    'args_json', payload['args_json'],
    'kwargs_json', payload['kwargs_json'],
    'queue', payload['queue'],
    'enabled', payload['enabled'],
    'overlap_policy', payload['overlap_policy'],
    'misfire_policy', payload['misfire_policy'],
    'created_at', payload['created_at'],
    'updated_at', payload['updated_at']
)
set_optional_field(sched_key, 'name', rec_name)
set_optional_field(sched_key, 'expression', payload['expression'])
set_optional_field(sched_key, 'interval_seconds', payload['interval_seconds'])
set_optional_field(sched_key, 'run_at', payload['run_at'])
set_optional_field(sched_key, 'lock_ttl', payload['lock_ttl'])
set_optional_field(sched_key, 'signature', payload['signature'])
set_optional_field(sched_key, 'grace_seconds', payload['grace_seconds'])
set_optional_field(sched_key, 'next_run_at', payload['next_run_at'])
set_optional_field(sched_key, 'next_run_ts', payload['next_run_ts'])
set_optional_field(sched_key, 'last_run_at', payload['last_run_at'])
set_optional_field(sched_key, 'last_claimed_occurrence', payload['last_claimed_occurrence'])
set_optional_field(sched_key, 'last_occurrence_id', payload['last_occurrence_id'])
set_optional_field(sched_key, 'last_status', payload['last_status'])
if payload['has_last_error'] == '1' then
    redis.call('HSET', sched_key, 'last_error', payload['last_error'])
else
    redis.call('HDEL', sched_key, 'last_error')
end

sync_due_index(due_key, rec_id, payload['enabled'], payload['next_run_ts'])
return {'ok', redis.call('HGETALL', sched_key)}
"""
)

DELETE_SCHEDULE_LUA = (
    _LUA_HELPERS
    + """
local schedules_key = KEYS[1]
local names_key = KEYS[2]
local due_key = KEYS[3]
local pending_key = KEYS[4]

local prefix = ARGV[1]
local lookup = ARGV[2]
local now_iso = ARGV[3]
local retention_sec = tonumber(ARGV[4]) or 604800

local sid = nil
if redis.call('EXISTS', prefix .. 'schedule:' .. lookup) == 1 then
    sid = lookup
else
    local mapped_id = redis.call('HGET', names_key, lookup)
    if mapped_id then
        if redis.call('EXISTS', prefix .. 'schedule:' .. mapped_id) == 1 then
            sid = mapped_id
        else
            redis.call('HDEL', names_key, lookup)
        end
    end
end

if not sid then
    return 0
end

local sched_key = prefix .. 'schedule:' .. sid
local name = redis.call('HGET', sched_key, 'name') or ''
if name ~= '' then
    redis.call('HDEL', names_key, name)
end

redis.call('SREM', schedules_key, sid)
redis.call('ZREM', due_key, sid)
redis.call('DEL', sched_key)

local occ_index_key = prefix .. 'sched_occs:' .. sid
local occ_ids = redis.call('ZRANGE', occ_index_key, 0, -1)
for _, occ_id in ipairs(occ_ids) do
    local occ_key = prefix .. 'occ:' .. occ_id
    if redis.call('EXISTS', occ_key) == 1 then
        local status = redis.call('HGET', occ_key, 'status')
        if status == 'pending' or status == 'claimed' or status == 'dispatch_pending' then
            redis.call('HSET', occ_key, 'status', 'cancelled', 'finished_at', now_iso)
            redis.call('HDEL', occ_key, 'lease_until')
            redis.call('ZREM', pending_key, occ_id)
            local lock_key = redis.call('HGET', occ_key, 'lock_key')
            local lock_token = redis.call('HGET', occ_key, 'lock_token')
            release_lock(lock_key, lock_token)
            if retention_sec > 0 then
                redis.call('EXPIRE', occ_key, retention_sec)
            end
        end
    end
end
redis.call('DEL', occ_index_key)
return 1
"""
)

SET_ENABLED_LUA = """
local names_key = KEYS[1]
local due_key = KEYS[2]

local prefix = ARGV[1]
local lookup = ARGV[2]
local enabled_val = ARGV[3]
local has_next = ARGV[4] == '1'
local next_run_iso = ARGV[5]
local next_run_ts = ARGV[6]
local updated_at = ARGV[7]

local sid = nil
if redis.call('EXISTS', prefix .. 'schedule:' .. lookup) == 1 then
    sid = lookup
else
    local mapped_id = redis.call('HGET', names_key, lookup)
    if mapped_id and redis.call('EXISTS', prefix .. 'schedule:' .. mapped_id) == 1 then
        sid = mapped_id
    end
end

if not sid then
    return {'err', 'not_found', lookup}
end

local sched_key = prefix .. 'schedule:' .. sid
redis.call('HSET', sched_key, 'enabled', enabled_val, 'updated_at', updated_at)

if enabled_val == '1' then
    local target_ts = nil
    if has_next and next_run_iso ~= '' then
        redis.call('HSET', sched_key, 'next_run_at', next_run_iso, 'next_run_ts', next_run_ts)
        target_ts = next_run_ts
    else
        local stored_ts = redis.call('HGET', sched_key, 'next_run_ts')
        if stored_ts and stored_ts ~= '' then
            target_ts = stored_ts
        end
    end
    if target_ts and target_ts ~= '' then
        redis.call('ZADD', due_key, tonumber(target_ts), sid)
    end
else
    redis.call('ZREM', due_key, sid)
end

return {'ok', redis.call('HGETALL', sched_key)}
"""

CLAIM_DUE_LUA = (
    _LUA_HELPERS
    + """
local due_key = KEYS[1]
local pending_key = KEYS[2]

local prefix = ARGV[1]
local now_iso = ARGV[2]
local claimed_by = ARGV[3]
local retention_sec = tonumber(ARGV[4]) or 604800
local max_history = tonumber(ARGV[5]) or 200
local candidates = cjson.decode(ARGV[6])

local function write_occ(occ_key, sid, occ_id, sched_key, c, status_val)
    local task_name = redis.call('HGET', sched_key, 'task_name') or ''
    local task_kind = redis.call('HGET', sched_key, 'task_kind') or 'job'
    local args_json = redis.call('HGET', sched_key, 'args_json') or '[]'
    local kwargs_json = redis.call('HGET', sched_key, 'kwargs_json') or '{}'
    local queue = redis.call('HGET', sched_key, 'queue') or 'default'
    local signature = redis.call('HGET', sched_key, 'signature') or ''

    redis.call('DEL', occ_key)
    redis.call('HSET', occ_key,
        'occurrence_id', occ_id,
        'schedule_id', sid,
        'scheduled_for', c['scheduled_for'],
        'status', status_val,
        'task_name', task_name,
        'task_kind', task_kind,
        'args_json', args_json,
        'kwargs_json', kwargs_json,
        'queue', queue,
        'signature', signature,
        'claimed_by', claimed_by,
        'claimed_at', now_iso
    )
end

local function record_occ_index(sid, occ_id, scheduled_for_ts)
    local occ_index_key = prefix .. 'sched_occs:' .. sid
    redis.call('ZADD', occ_index_key, tonumber(scheduled_for_ts), occ_id)
    if max_history > 0 then
        local card = tonumber(redis.call('ZCARD', occ_index_key)) or 0
        if card > max_history then
            local excess = card - max_history
            local oldest = redis.call('ZRANGE', occ_index_key, 0, excess - 1)
            for _, old_id in ipairs(oldest) do
                local old_occ_key = prefix .. 'occ:' .. old_id
                local old_status = redis.call('HGET', old_occ_key, 'status')
                if (not old_status) or old_status == 'completed' or old_status == 'failed' or old_status == 'skipped' or old_status == 'cancelled' then
                    redis.call('ZREM', occ_index_key, old_id)
                    redis.call('DEL', old_occ_key)
                end
            end
        end
    end
end

local function advance_schedule(sched_key, sid, occ_id, status_val, c)
    redis.call('HSET', sched_key,
        'last_claimed_occurrence', occ_id,
        'last_occurrence_id', occ_id,
        'last_status', status_val,
        'updated_at', now_iso
    )
    if c['is_once'] then
        redis.call('HSET', sched_key, 'enabled', '0')
        redis.call('HDEL', sched_key, 'next_run_at', 'next_run_ts')
        redis.call('ZREM', due_key, sid)
    elseif c['next_run_at'] and c['next_run_at'] ~= '' and c['next_run_ts'] and c['next_run_ts'] ~= '' then
        redis.call('HSET', sched_key, 'next_run_at', c['next_run_at'], 'next_run_ts', c['next_run_ts'])
        redis.call('ZADD', due_key, tonumber(c['next_run_ts']), sid)
    else
        redis.call('HDEL', sched_key, 'next_run_at', 'next_run_ts')
        redis.call('ZREM', due_key, sid)
    end
end

local outcomes = {}

for _, c in ipairs(candidates) do
    local sid = c['id']
    local occ_id = c['occ_id']
    local sched_key = prefix .. 'schedule:' .. sid

    if redis.call('EXISTS', sched_key) == 1 then
        local cur_enabled = redis.call('HGET', sched_key, 'enabled')
        local cur_next_run = redis.call('HGET', sched_key, 'next_run_at')
        local cur_last_claimed = redis.call('HGET', sched_key, 'last_claimed_occurrence')

        if cur_enabled ~= '1' then
            redis.call('ZREM', due_key, sid)
        elseif cur_next_run == c['expected_next_run'] and cur_last_claimed ~= occ_id then
            local occ_key = prefix .. 'occ:' .. occ_id

            if c['is_misfire_skip'] then
                write_occ(occ_key, sid, occ_id, sched_key, c, 'skipped')
                redis.call('HSET', occ_key,
                    'finished_at', now_iso,
                    'dispatch_attempts', '0',
                    'error', 'Skipped due to misfire grace window exceeded'
                )
                if retention_sec > 0 then
                    redis.call('EXPIRE', occ_key, retention_sec)
                end
                record_occ_index(sid, occ_id, c['scheduled_for_ts'])
                advance_schedule(sched_key, sid, occ_id, 'skipped', c)
                table.insert(outcomes, {
                    'skipped',
                    'misfire',
                    redis.call('HGETALL', sched_key),
                    redis.call('HGETALL', occ_key)
                })
            else
                local lock_ok = true
                local lock_key = c['lock_key'] or ''
                local lock_token = c['lock_token'] or ''
                local lock_ttl = tonumber(c['lock_ttl']) or 0

                if c['overlap_skip'] then
                    local ttl_ms = math.max(1, lock_ttl * 1000)
                    local set_res = redis.call('SET', lock_key, lock_token, 'NX', 'PX', ttl_ms)
                    lock_ok = (set_res == true)
                        or (type(set_res) == 'table' and set_res.ok == 'OK')
                        or (type(set_res) == 'string' and set_res == 'OK')
                end

                if not lock_ok then
                    write_occ(occ_key, sid, occ_id, sched_key, c, 'skipped')
                    redis.call('HSET', occ_key,
                        'finished_at', now_iso,
                        'dispatch_attempts', '0',
                        'error', 'Skipped due to overlap lock held by active occurrence'
                    )
                    if retention_sec > 0 then
                        redis.call('EXPIRE', occ_key, retention_sec)
                    end
                    record_occ_index(sid, occ_id, c['scheduled_for_ts'])
                    advance_schedule(sched_key, sid, occ_id, 'skipped', c)
                    table.insert(outcomes, {
                        'skipped',
                        'overlap',
                        redis.call('HGETALL', sched_key),
                        redis.call('HGETALL', occ_key)
                    })
                else
                    write_occ(occ_key, sid, occ_id, sched_key, c, 'dispatch_pending')
                    redis.call('HSET', occ_key,
                        'lease_until', c['lease_until'],
                        'dispatch_attempts', '1'
                    )
                    if c['overlap_skip'] then
                        redis.call('HSET', occ_key,
                            'lock_key', lock_key,
                            'lock_token', lock_token,
                            'lock_ttl', tostring(lock_ttl)
                        )
                    end
                    redis.call('ZADD', pending_key, tonumber(c['lease_until']), occ_id)
                    record_occ_index(sid, occ_id, c['scheduled_for_ts'])
                    advance_schedule(sched_key, sid, occ_id, 'dispatch_pending', c)
                    table.insert(outcomes, {
                        'claimed',
                        '',
                        redis.call('HGETALL', sched_key),
                        redis.call('HGETALL', occ_key)
                    })
                end
            end
        end
    end
end

return outcomes
"""
)

CONFIRM_DISPATCHED_LUA = (
    _LUA_HELPERS
    + """
local occ_key = KEYS[1]
local pending_key = KEYS[2]

local prefix = ARGV[1]
local occ_id = ARGV[2]
local job_id = ARGV[3]
local now_iso = ARGV[4]

if redis.call('EXISTS', occ_key) == 0 then
    redis.call('ZREM', pending_key, occ_id)
    return {}
end

redis.call('ZREM', pending_key, occ_id)
local status = redis.call('HGET', occ_key, 'status')
if status == 'cancelled' then
    local lock_key = redis.call('HGET', occ_key, 'lock_key')
    local lock_token = redis.call('HGET', occ_key, 'lock_token')
    release_lock(lock_key, lock_token)
    return redis.call('HGETALL', occ_key)
end
if status ~= 'pending' and status ~= 'claimed' and status ~= 'dispatch_pending' then
    if status == 'failed' or status == 'skipped' then
        local lock_key = redis.call('HGET', occ_key, 'lock_key')
        local lock_token = redis.call('HGET', occ_key, 'lock_token')
        release_lock(lock_key, lock_token)
    end
    return redis.call('HGETALL', occ_key)
end

redis.call('HSET', occ_key,
    'status', 'dispatched',
    'job_id', job_id,
    'dispatched_at', now_iso
)
redis.call('HDEL', occ_key, 'lease_until', 'error')

local sched_id = redis.call('HGET', occ_key, 'schedule_id')
local scheduled_for = redis.call('HGET', occ_key, 'scheduled_for')
if sched_id then
    local sched_key = prefix .. 'schedule:' .. sched_id
    if redis.call('EXISTS', sched_key) == 1 then
        redis.call('HSET', sched_key,
            'last_occurrence_id', occ_id,
            'last_status', 'dispatched',
            'updated_at', now_iso
        )
        if scheduled_for then
            redis.call('HSET', sched_key, 'last_run_at', scheduled_for)
        end
        redis.call('HDEL', sched_key, 'last_error')
    end
end

return redis.call('HGETALL', occ_key)
"""
)

RECLAIM_PENDING_LUA = (
    _LUA_HELPERS
    + """
local pending_key = KEYS[1]

local prefix = ARGV[1]
local now_ts = ARGV[2]
local now_iso = ARGV[3]
local reclaimed_by = ARGV[4]
local next_lease_until = ARGV[5]
local max_attempts = tonumber(ARGV[6]) or 5
local limit = tonumber(ARGV[7]) or 50
local retention_sec = tonumber(ARGV[8]) or 604800

local expired = redis.call('ZRANGEBYSCORE', pending_key, '-inf', tonumber(now_ts), 'LIMIT', 0, limit)
local results = {}

for _, occ_id in ipairs(expired) do
    local occ_key = prefix .. 'occ:' .. occ_id
    if redis.call('EXISTS', occ_key) == 0 then
        redis.call('ZREM', pending_key, occ_id)
    else
        local status = redis.call('HGET', occ_key, 'status')
        if status ~= 'dispatch_pending' then
            redis.call('ZREM', pending_key, occ_id)
        else
            local attempts = tonumber(redis.call('HGET', occ_key, 'dispatch_attempts')) or 0
            if attempts >= max_attempts then
                local err = redis.call('HGET', occ_key, 'error')
                if (not err) or err == '' then
                    err = 'Dispatch failed after ' .. tostring(attempts) .. ' pending attempts'
                end
                redis.call('HSET', occ_key,
                    'status', 'failed',
                    'finished_at', now_iso,
                    'error', err
                )
                redis.call('HDEL', occ_key, 'lease_until')
                redis.call('ZREM', pending_key, occ_id)

                local lock_key = redis.call('HGET', occ_key, 'lock_key')
                local lock_token = redis.call('HGET', occ_key, 'lock_token')
                release_lock(lock_key, lock_token)

                if retention_sec > 0 then
                    redis.call('EXPIRE', occ_key, retention_sec)
                end

                local sched_id = redis.call('HGET', occ_key, 'schedule_id')
                if sched_id then
                    local sched_key = prefix .. 'schedule:' .. sched_id
                    if redis.call('EXISTS', sched_key) == 1 then
                        redis.call('HSET', sched_key,
                            'last_status', 'failed',
                            'last_error', err,
                            'updated_at', now_iso
                        )
                    end
                end
                table.insert(results, {'exhausted', redis.call('HGETALL', occ_key)})
            else
                attempts = attempts + 1
                redis.call('HSET', occ_key,
                    'dispatch_attempts', tostring(attempts),
                    'claimed_by', reclaimed_by,
                    'lease_until', next_lease_until
                )
                redis.call('ZADD', pending_key, tonumber(next_lease_until), occ_id)
                table.insert(results, {'retry', redis.call('HGETALL', occ_key)})
            end
        end
    end
end

return results
"""
)

MARK_DISPATCH_FAILED_LUA = (
    _LUA_HELPERS
    + """
local occ_key = KEYS[1]
local pending_key = KEYS[2]

local prefix = ARGV[1]
local occ_id = ARGV[2]
local err = ARGV[3]
local now_ts = ARGV[4]
local now_iso = ARGV[5]
local max_attempts = tonumber(ARGV[6]) or 5
local retention_sec = tonumber(ARGV[7]) or 604800

if redis.call('EXISTS', occ_key) == 0 then
    redis.call('ZREM', pending_key, occ_id)
    return {}
end

local status = redis.call('HGET', occ_key, 'status')
if status ~= 'pending' and status ~= 'claimed' and status ~= 'dispatch_pending' then
    redis.call('ZREM', pending_key, occ_id)
    return redis.call('HGETALL', occ_key)
end

redis.call('HSET', occ_key, 'error', err)
local attempts = tonumber(redis.call('HGET', occ_key, 'dispatch_attempts')) or 0

if attempts >= max_attempts then
    redis.call('HSET', occ_key,
        'status', 'failed',
        'finished_at', now_iso
    )
    redis.call('HDEL', occ_key, 'lease_until')
    redis.call('ZREM', pending_key, occ_id)

    local lock_key = redis.call('HGET', occ_key, 'lock_key')
    local lock_token = redis.call('HGET', occ_key, 'lock_token')
    release_lock(lock_key, lock_token)

    if retention_sec > 0 then
        redis.call('EXPIRE', occ_key, retention_sec)
    end

    local sched_id = redis.call('HGET', occ_key, 'schedule_id')
    if sched_id then
        local sched_key = prefix .. 'schedule:' .. sched_id
        if redis.call('EXISTS', sched_key) == 1 then
            redis.call('HSET', sched_key,
                'last_status', 'failed',
                'last_error', err,
                'updated_at', now_iso
            )
        end
    end
else
    redis.call('HSET', occ_key, 'lease_until', now_ts)
    redis.call('ZADD', pending_key, tonumber(now_ts), occ_id)
end

return redis.call('HGETALL', occ_key)
"""
)

MARK_RUNNING_LUA = """
local occ_key = KEYS[1]
local pending_key = KEYS[2]

local prefix = ARGV[1]
local occ_id = ARGV[2]
local now_iso = ARGV[3]

if redis.call('EXISTS', occ_key) == 0 then
    return {}
end

redis.call('ZREM', pending_key, occ_id)
local status = redis.call('HGET', occ_key, 'status')
if status == 'completed' or status == 'failed' or status == 'skipped' or status == 'cancelled' then
    return redis.call('HGETALL', occ_key)
end
local existing_started = redis.call('HGET', occ_key, 'started_at')
redis.call('HSET', occ_key,
    'status', 'running',
    'started_at', (existing_started and existing_started ~= '') and existing_started or now_iso
)
redis.call('HDEL', occ_key, 'lease_until')

-- Re-arm the overlap lock so queue wait time does not consume the run budget.
local run_lock_key = redis.call('HGET', occ_key, 'lock_key')
local run_lock_token = redis.call('HGET', occ_key, 'lock_token')
local run_lock_ttl = tonumber(redis.call('HGET', occ_key, 'lock_ttl') or '')
if run_lock_key and run_lock_token and run_lock_ttl and run_lock_ttl > 0 then
    if redis.call('GET', run_lock_key) == run_lock_token then
        redis.call('PEXPIRE', run_lock_key, math.max(1, math.floor(run_lock_ttl * 1000)))
    end
end
local sched_id = redis.call('HGET', occ_key, 'schedule_id')
if sched_id then
    local sched_key = prefix .. 'schedule:' .. sched_id
    if redis.call('EXISTS', sched_key) == 1 then
        redis.call('HSET', sched_key,
            'last_status', 'running',
            'updated_at', now_iso
        )
    end
end

return redis.call('HGETALL', occ_key)
"""

MARK_COMPLETED_LUA = (
    _LUA_HELPERS
    + """
local occ_key = KEYS[1]
local pending_key = KEYS[2]

local prefix = ARGV[1]
local occ_id = ARGV[2]
local now_iso = ARGV[3]
local retention_sec = tonumber(ARGV[4]) or 604800

if redis.call('EXISTS', occ_key) == 0 then
    return {}
end

redis.call('ZREM', pending_key, occ_id)
local status = redis.call('HGET', occ_key, 'status')
if status == 'failed' or status == 'cancelled' or status == 'skipped' then
    return redis.call('HGETALL', occ_key)
end
if status == 'completed' then
    local lock_key = redis.call('HGET', occ_key, 'lock_key')
    local lock_token = redis.call('HGET', occ_key, 'lock_token')
    release_lock(lock_key, lock_token)
    return redis.call('HGETALL', occ_key)
end
redis.call('HSET', occ_key,
    'status', 'completed',
    'finished_at', now_iso
)
redis.call('HDEL', occ_key, 'lease_until', 'error')

local lock_key = redis.call('HGET', occ_key, 'lock_key')
local lock_token = redis.call('HGET', occ_key, 'lock_token')
release_lock(lock_key, lock_token)

if retention_sec > 0 then
    redis.call('EXPIRE', occ_key, retention_sec)
end

local sched_id = redis.call('HGET', occ_key, 'schedule_id')
local scheduled_for = redis.call('HGET', occ_key, 'scheduled_for')
if sched_id then
    local sched_key = prefix .. 'schedule:' .. sched_id
    if redis.call('EXISTS', sched_key) == 1 then
        local existing_last_run = redis.call('HGET', sched_key, 'last_run_at')
        if ((not existing_last_run) or existing_last_run == '') and scheduled_for then
            redis.call('HSET', sched_key, 'last_run_at', scheduled_for)
        end
        redis.call('HSET', sched_key,
            'last_occurrence_id', occ_id,
            'last_status', 'completed',
            'updated_at', now_iso
        )
        redis.call('HDEL', sched_key, 'last_error')
    end
end

return redis.call('HGETALL', occ_key)
"""
)

MARK_FAILED_LUA = (
    _LUA_HELPERS
    + """
local occ_key = KEYS[1]
local pending_key = KEYS[2]

local prefix = ARGV[1]
local occ_id = ARGV[2]
local err = ARGV[3]
local now_iso = ARGV[4]
local retention_sec = tonumber(ARGV[5]) or 604800

if redis.call('EXISTS', occ_key) == 0 then
    return {}
end

redis.call('ZREM', pending_key, occ_id)
local status = redis.call('HGET', occ_key, 'status')
if status == 'completed' or status == 'cancelled' or status == 'skipped' then
    return redis.call('HGETALL', occ_key)
end
if status == 'failed' then
    local lock_key = redis.call('HGET', occ_key, 'lock_key')
    local lock_token = redis.call('HGET', occ_key, 'lock_token')
    release_lock(lock_key, lock_token)
    return redis.call('HGETALL', occ_key)
end
redis.call('HSET', occ_key,
    'status', 'failed',
    'finished_at', now_iso,
    'error', err
)
redis.call('HDEL', occ_key, 'lease_until')

local lock_key = redis.call('HGET', occ_key, 'lock_key')
local lock_token = redis.call('HGET', occ_key, 'lock_token')
release_lock(lock_key, lock_token)

if retention_sec > 0 then
    redis.call('EXPIRE', occ_key, retention_sec)
end

local sched_id = redis.call('HGET', occ_key, 'schedule_id')
if sched_id then
    local sched_key = prefix .. 'schedule:' .. sched_id
    if redis.call('EXISTS', sched_key) == 1 then
        redis.call('HSET', sched_key,
            'last_occurrence_id', occ_id,
            'last_status', 'failed',
            'last_error', err,
            'updated_at', now_iso
        )
    end
end

return redis.call('HGETALL', occ_key)
"""
)

RELEASE_OVERLAP_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

EXTEND_OVERLAP_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], math.max(1, tonumber(ARGV[2])))
end
return 0
"""

__all__ = [
    "CLAIM_DUE_LUA",
    "CONFIRM_DISPATCHED_LUA",
    "DELETE_SCHEDULE_LUA",
    "EXTEND_OVERLAP_LOCK_LUA",
    "GET_SCHEDULE_LUA",
    "MARK_COMPLETED_LUA",
    "MARK_DISPATCH_FAILED_LUA",
    "MARK_FAILED_LUA",
    "MARK_RUNNING_LUA",
    "NULLABLE_SCHEDULE_FIELDS",
    "RECLAIM_PENDING_LUA",
    "RELEASE_OVERLAP_LOCK_LUA",
    "SAVE_SCHEDULE_LUA",
    "SET_ENABLED_LUA",
    "UPDATE_SCHEDULE_LUA",
    "flat_list_to_dict",
    "hash_to_occurrence",
    "hash_to_schedule",
    "schedule_to_lua_payload",
]
