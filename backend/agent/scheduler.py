"""Calendar boundaries are independent of polling and model completion time."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def next_boundary(now, hour=19, minute=0, timezone='Asia/Shanghai'):
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def latest_boundary(now, hour=19, minute=0, timezone='Asia/Shanghai'):
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() > now:
        candidate -= timedelta(days=1)
    return candidate.timestamp()
