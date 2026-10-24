"""US Eastern time helpers — prop firms and CME quote their hours in ET.
Daylight saving is handled by the tz database, so nothing needs changing in November/March."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .events import NS

ET = ZoneInfo("America/New_York")


def hhmm(text: str) -> int:
    """'16:59' -> minutes after midnight."""
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


class EtClock:
    def __init__(self, day_reset_min: int):
        self.reset = day_reset_min
        self._cache = {}

    def offset(self, ts: int) -> int:
        hour = ts // (3600 * NS)
        off = self._cache.get(hour)
        if off is None:
            dt = datetime.fromtimestamp(hour * 3600, tz=timezone.utc).astimezone(ET)
            off = int(dt.utcoffset().total_seconds())
            self._cache[hour] = off
        return off

    def minute_of_day(self, ts: int) -> int:
        return ((ts // NS + self.offset(ts)) // 60) % 1440

    def day_id(self, ts: int) -> int:
        """Trading-day number. A day starts at day_reset ET (18:00 on CME)."""
        return (ts // NS + self.offset(ts) + (1440 - self.reset) * 60) // 86400
