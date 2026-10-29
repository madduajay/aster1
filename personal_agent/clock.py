"""Authoritative runtime clock; never derive today's date from model knowledge."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config


def clock_context(zone=None, *, now=None):
    name = zone or config.TIMEZONE
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Use a valid IANA timezone, for example America/Phoenix') from None
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError('Clock instant must include a timezone')
    local = instant.astimezone(tz)
    return {'timezone':name, 'local_datetime':local.isoformat(timespec='seconds'), 'date':local.date().isoformat(),
            'weekday':local.strftime('%A'), 'utc_datetime':instant.astimezone(timezone.utc).isoformat(timespec='seconds'),
            'tomorrow':(local.date()+timedelta(days=1)).isoformat()}
