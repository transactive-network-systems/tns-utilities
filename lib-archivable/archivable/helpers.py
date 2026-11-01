"""Small conversions the archive needs: aware datetimes and durations given as seconds."""
from datetime import datetime, timedelta, tzinfo


def require_aware(value: datetime, name: str = 'datetime', assume: tzinfo | None = None) -> datetime:
    """
    Return `value` if it is a timezone-aware datetime. A naive value is given the `assume` zone when one is supplied
    (the documented convention at that boundary) and is otherwise an error: the archive stores UTC, and a naive value
    that slips in is read back in whatever zone the reader happens to assume.
    """
    if not isinstance(value, datetime):
        raise TypeError(f'{name} must be a datetime, not {type(value).__name__}')
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        if assume is None:
            raise ValueError(f'{name} must be timezone-aware; got naive {value.isoformat()}')
        return value.replace(tzinfo=assume)
    return value


def parse_timedelta(value) -> timedelta | None:
    """timedelta from a timedelta or a number of seconds (the form durations take on the wire and in configuration)."""
    if value is None or isinstance(value, timedelta):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'Unrecognised duration: {value!r}. Give a number of seconds.')
    return timedelta(seconds=float(value))
