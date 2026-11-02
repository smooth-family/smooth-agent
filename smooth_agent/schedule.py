"""Schedules for cron jobs: parse a schedule string, compute the next run.

Accepted forms (times are in the configured timezone, default the machine's):
  "*/15 * * * *"            standard 5-field cron (minute hour day-of-month month day-of-week;
                            lists, ranges, steps, names like mon / jan; 0 and 7 are Sunday)
  "@hourly" "@daily" "@weekly" "@monthly"
  "every 30m" / "every 2h" / "every 1d"   fixed interval (minimum 1 minute)
  "daily 09:00"  "weekdays 08:30"  "weekly mon 09:00"
  "at 2026-11-03 09:00"     once (ISO date and time)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}


class ScheduleError(ValueError):
    pass


def get_tz(name: str | None) -> tzinfo:
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            raise ScheduleError(f"unknown timezone: {name}") from None
    return datetime.now().astimezone().tzinfo or timezone.utc


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    out: set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ScheduleError(f"bad step in '{text}'")
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _val(x, names), _val(y, names)
        else:
            a = _val(part, names)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ScheduleError(f"'{text}' is out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


def _val(x: str, names: dict[str, int] | None) -> int:
    x = x.strip()
    if names and x[:3] in names:
        return names[x[:3]]
    if not x.isdigit():
        raise ScheduleError(f"bad value '{x}'")
    return int(x)


@dataclass
class Schedule:
    kind: str  # cron | interval | once
    text: str
    minutes: set[int] | None = None
    hours: set[int] | None = None
    dom: set[int] | None = None
    months: set[int] | None = None
    dow: set[int] | None = None
    dom_any: bool = True
    dow_any: bool = True
    interval: int = 0  # seconds
    at: datetime | None = None

    def describe(self) -> str:
        if self.kind == "interval":
            return f"every {self.interval // 60} min" if self.interval < 3600 else f"every {self.interval / 3600:g} h"
        if self.kind == "once":
            return f"once at {self.at:%Y-%m-%d %H:%M}"
        return f"cron '{self.text}'"

    def next_after(self, after: datetime, tz: tzinfo) -> datetime | None:
        """First run time strictly after `after` (aware datetime), or None if there is none."""
        if self.kind == "once":
            at = self.at.replace(tzinfo=tz) if self.at.tzinfo is None else self.at
            return at if at > after else None
        if self.kind == "interval":
            return after + timedelta(seconds=self.interval)
        t = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 5):
            if t.month in self.months and self._day_ok(t):
                hours = sorted(h for h in self.hours if h >= t.hour)
                for h in hours:
                    mins = sorted(m for m in self.minutes if (h > t.hour or m >= t.minute))
                    if mins:
                        return t.replace(hour=h, minute=mins[0])
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        return None

    def _day_ok(self, t: datetime) -> bool:
        dow = (t.weekday() + 1) % 7  # Sunday = 0
        dom_ok = t.day in self.dom
        dow_ok = dow in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok  # cron: either restricted field may match


def parse(text: str) -> Schedule:
    raw = " ".join(str(text or "").strip().split())
    s = raw.lower()
    if not s:
        raise ScheduleError("empty schedule")
    s = _ALIASES.get(s, s)
    m = re.fullmatch(r"every\s+(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        if secs < 60:
            raise ScheduleError("the shortest interval is 1 minute")
        return Schedule(kind="interval", text=raw, interval=secs)
    m = re.fullmatch(r"at\s+(\d{4}-\d{2}-\d{2})[ t](\d{1,2}:\d{2})", s)
    if m:
        return Schedule(kind="once", text=raw, at=datetime.fromisoformat(f"{m.group(1)}T{m.group(2).zfill(5)}"))
    m = re.fullmatch(r"(daily|weekdays|weekends|weekly\s+([a-z]{3})[a-z]*)\s+(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(3)), int(m.group(4))
        dow = {"daily": "*", "weekdays": "1-5", "weekends": "0,6"}.get(m.group(1)) or str(_DOW.get(m.group(2) or "", -1))
        if dow == "-1":
            raise ScheduleError(f"unknown day in '{raw}'")
        s = f"{mi} {h} * * {dow}"
    parts = s.split()
    if len(parts) != 5:
        raise ScheduleError(f"can't read schedule '{raw}' (use cron like '0 9 * * 1-5', 'every 30m', 'daily 09:00' or 'at 2026-11-03 09:00')")
    mi, h, dom, mon, dow = parts
    dow_set = {d % 7 for d in _field(dow, 0, 7, _DOW)}
    return Schedule(kind="cron", text=raw, minutes=_field(mi, 0, 59), hours=_field(h, 0, 23), dom=_field(dom, 1, 31),
                    months=_field(mon, 1, 12, _MON), dow=dow_set, dom_any=dom == "*", dow_any=dow == "*")


def next_run(text: str, after: float, tz_name: str | None = None) -> float | None:
    """Epoch seconds of the next run after `after` (epoch), or None."""
    tz = get_tz(tz_name)
    nxt = parse(text).next_after(datetime.fromtimestamp(after, tz), tz)
    return nxt.timestamp() if nxt else None


__all__ = ["Schedule", "ScheduleError", "parse", "next_run", "get_tz"]
