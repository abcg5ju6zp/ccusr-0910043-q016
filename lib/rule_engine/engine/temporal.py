#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/engine/temporal.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

"""
时态快照（temporal snapshot）。

续保等规则同时引用保单时区、节假日与宽限期，若在求值过程中直接读取墙上时钟，
同一条规则在几分钟后批量重放就可能得到不同结果。:py:class:`TemporalSnapshot`
把一次评估所需的全部时态输入固定为不可变值：

* **业务时刻**（``now``）——规则看到的“现在”，与系统墙上时钟解耦；
* **时区数据库版本**（:py:attr:`TemporalSnapshot.timezone_database_version`）——
  解析时区与夏令时规则所用的 tzdata/IANA 版本；
* **日历版本**（:py:attr:`TemporalSnapshot.calendar_version`）——
  节假日/工作日表的版本及其内容。

规则中的窗口（``window_*``）、日期截断（``start_of_day`` 等）与持续时间运算都从
快照推导，因此同一份规则 + 同一份输入 + 同一份快照必然得到同一结果，调用方可以
把快照随决定持久化并在日后原样重放。

快照经 :py:meth:`TemporalSnapshot.to_dict` / :py:meth:`TemporalSnapshot.from_dict`
序列化为普通 JSON 兼容数据，随业务决定落库；重放时调用
:py:meth:`TemporalSnapshot.compatible_with` 或比较 :py:attr:`TemporalSnapshot.fingerprint`
即可确认使用的版本。
"""

from __future__ import annotations

import datetime
import enum
import functools
import hashlib
import json
import threading
from typing import Any, Callable, Iterable, Mapping

import dateutil.tz

from ..errors import EvaluationError, FunctionCallError

__all__ = (
    'Calendar',
    'DstResolutionPolicy',
    'TemporalError',
    'TemporalSnapshot',
    'detect_timezone_database_version',
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TemporalError(EvaluationError):
    """时态运算错误（非法构造点、快照版本不匹配等）。"""
    def __init__(
            self,
            message: str,
            *,
            instant: datetime.datetime | None = None,
            timezone_name: str | None = None
    ) -> None:
        super().__init__(message)
        self.instant = instant
        """触发错误的墙上时刻（如夏令时间隙中不存在的本地时间）。"""
        self.timezone_name = timezone_name
        """触发错误的时区名称。"""

# ---------------------------------------------------------------------------
# DST 语义
# ---------------------------------------------------------------------------
class DstResolutionPolicy(str, enum.Enum):
    """夏令时重叠（fold）与缺失（gap）本地时刻的解析策略。"""
    RAISE = 'raise'
    """缺失或重叠时刻一律抛出 :py:exc:`TemporalError`（默认，最严格）。"""
    RAISE_ON_GAP = 'raise_on_gap'
    """缺失时刻抛出；重叠时刻采用日间侧（``fold=0``，与 dateutil/zoneinfo 默认一致）。"""
    SHIFT_FORWARD = 'shift_forward'
    """缺失时刻向后推移到过渡结束点；重叠时刻采用日间侧。"""
    SHIFT_BACKWARD = 'shift_backward'
    """缺失时刻向前推移到过渡开始点；重叠时刻采用标准侧（``fold=1``）。"""

    @property
    def raises_on_gap(self) -> bool:
        return self is DstResolutionPolicy.RAISE or self is DstResolutionPolicy.RAISE_ON_GAP

    @property
    def raises_on_fold(self) -> bool:
        return self is DstResolutionPolicy.RAISE

# ---------------------------------------------------------------------------
# Timezone database version detection
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def detect_timezone_database_version() -> str:
    """
    返回当前进程解析时区所实际使用的时区数据库版本标识。

    dateutil 优先读取系统 zoneinfo 文件，其次才是 pip ``tzdata`` 包，因此这里
    通过实际解析一个时区得到其 tzfile 路径再判断来源：

    1. tzfile 位于 pip ``tzdata`` 包目录内 → ``'tzdata/<包版本>'``；
    2. tzfile 位于系统 zoneinfo 目录，且 :file:`+VERSION` 可读 → ``'system/iana-<版本>'``；
    3. 其它情况（Windows 注册表、无法定位文件等）→ ``'unknown'``，调用方可自行指定。
    """
    probe = dateutil.tz.gettz('America/New_York') or dateutil.tz.gettz('UTC')
    tzfile_path = getattr(probe, '_filename', None)
    if tzfile_path:
        tzdata_version = _pip_tzdata_version()
        if tzdata_version is not None:
            tzdata_root = _pip_tzdata_root()
            if tzdata_root is not None:
                import os
                if os.path.abspath(tzfile_path).startswith(tzdata_root):
                    return 'tzdata/' + tzdata_version
        system_version = _system_zoneinfo_version(tzfile_path)
        if system_version is not None:
            return 'system/iana-' + system_version
    return 'unknown'

def _pip_tzdata_root() -> str | None:
    try:
        import importlib
        import os
        tzdata_package = importlib.import_module('tzdata')
    except Exception:
        return None
    paths = list(getattr(tzdata_package, '__path__', []))
    return os.path.abspath(paths[0]) if paths else None

def _pip_tzdata_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version('tzdata')
        except PackageNotFoundError:
            return None
    except Exception:
        return None

def _system_zoneinfo_version(tzfile_path: str) -> str | None:
    import os
    root = os.path.abspath(tzfile_path)
    candidates = []
    for _ in range(6):
        root = os.path.dirname(root)
        candidates.append(os.path.join(root, '+VERSION'))
    for candidate in candidates:
        try:
            with open(candidate, 'r', encoding='ascii') as file_handle:
                return file_handle.read().strip()
        except OSError:
            continue
    # Debian/Ubuntu ship zoneinfo without +VERSION; the tzdata package version carries the IANA release
    dpkg_version = _dpkg_tzdata_version()
    if dpkg_version is not None:
        return 'debian-' + dpkg_version
    return None

@functools.lru_cache(maxsize=1)
def _dpkg_tzdata_version() -> str | None:
    try:
        import subprocess
        completed = subprocess.run(
                ('dpkg-query', '-W', '-f=${Version}', 'tzdata'),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False)
    except Exception:  # environment dependent (no dpkg, no executable, sandboxed subprocess)
        return None
    if completed.returncode != 0:
        return None
    version = completed.stdout.decode('ascii', 'ignore').strip()
    return version or None

# ---------------------------------------------------------------------------
# Calendar (holidays / business days)
# ---------------------------------------------------------------------------
class Calendar:
    """
    不可变日历：一组节假日日期，可选工作日集合。

    日历是纯值对象，按内容计算版本指纹；调用方也可以显式传入 *version*
    （例如节假日表的发布编号）。两个日历相等当且仅当版本、节假日集合与
    工作日集合全部相同。
    """
    __slots__ = ('__version', '__holidays', '__business_days', '__fingerprint')

    def __init__(
            self,
            holidays: Iterable[datetime.date] = (),
            *,
            business_days: Iterable[int] | None = None,
            version: str | None = None
    ) -> None:
        holiday_set = frozenset(holidays)
        for day in holiday_set:
            if not isinstance(day, datetime.date):
                raise TypeError("holiday entries must be datetime.date instances, got " + type(day).__name__)
        if business_days is None:
            business_day_set = frozenset(range(0, 7))
        else:
            business_day_set = frozenset(business_days)
            bad = [day for day in business_day_set if not (0 <= day <= 6)]
            if bad:
                raise ValueError('business_days values must be in range 0 (Monday) through 6 (Sunday)')
        self.__holidays = holiday_set
        self.__business_days = business_day_set
        digest = hashlib.sha256()
        digest.update(b'holidays:')
        for day in sorted(holiday_set):
            digest.update(day.isoformat().encode('ascii'))
            digest.update(b',')
        digest.update(b'business_days:')
        digest.update(','.join(str(d) for d in sorted(business_day_set)).encode('ascii'))
        content_hash = digest.hexdigest()[:16]
        self.__version = version or ('content-' + content_hash)
        self.__fingerprint = '{0}:{1}'.format(self.__version, content_hash)

    @property
    def version(self) -> str:
        """日历版本标识（显式版本号或基于内容的哈希版本）。"""
        return self.__version

    @property
    def fingerprint(self) -> str:
        """日历完整指纹（版本 + 内容哈希），用于重放时逐字节确认。"""
        return self.__fingerprint

    @property
    def holidays(self) -> frozenset[datetime.date]:
        return self.__holidays

    @property
    def business_days(self) -> frozenset[int]:
        return self.__business_days

    def is_holiday(self, day: datetime.date) -> bool:
        """*day* 是否为节假日。"""
        return day in self.__holidays

    def is_business_day(self, day: datetime.date) -> bool:
        """*day* 是否为工作日（在工作日集合中且不是节假日）。"""
        return (day.weekday() in self.__business_days) and day not in self.__holidays

    def add_business_days(self, day: datetime.date, count: int) -> datetime.date:
        """返回从 *day* 起向前（*count* 为负则向后）数 *count* 个工作日后的日期。"""
        if count == 0:
            while not self.is_business_day(day):
                day += datetime.timedelta(days=1 if count >= 0 else -1)
            return day
        step = 1 if count > 0 else -1
        remaining = abs(count)
        while remaining:
            day += datetime.timedelta(days=step)
            if self.is_business_day(day):
                remaining -= 1
        return day

    def to_dict(self) -> dict[str, Any]:
        return {
            'version': self.__version,
            'holidays': tuple(sorted(day.isoformat() for day in self.__holidays)),
            'business_days': tuple(sorted(self.__business_days)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> 'Calendar':
        return cls(
            (datetime.date.fromisoformat(day) for day in value.get('holidays', ())),
            business_days=value.get('business_days'),
            version=value.get('version'),
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Calendar):
            return NotImplemented
        return self.__fingerprint == other.__fingerprint

    def __hash__(self) -> int:
        return hash(self.__fingerprint)

    def __repr__(self) -> str:
        return "<{0} version={1!r} holidays={2} >".format(self.__class__.__name__, self.__version, len(self.__holidays))

# ---------------------------------------------------------------------------
# Temporal snapshot
# ---------------------------------------------------------------------------
class TemporalSnapshot:
    """
    一次规则评估的时态快照。

    :param now: 业务时刻。时区感知的 :py:class:`~datetime.datetime`；若无时区信息，
        按 *timezone*（缺省为 Context 默认时区）本地化。
    :param timezone: 保单/业务时区，用于日期截断、日历判断与无时区字面量的归属。
        可为 :py:class:`~datetime.tzinfo` 或 IANA 名称。
    :param calendar: 节假日/工作日日历，携带自身版本。
    :param timezone_database_version: 时区数据库版本；缺省自动探测。历史重放时
        应传入当时记录的版本以便核对（版本核对失败会在 :py:meth:`compatible_with`
        中显式暴露，而不会静默改变结果）。
    :param dst_policy: 夏令时间隙/重叠本地时刻的解析策略。
    """
    __slots__ = (
            '__now', '__timezone', '__timezone_name', '__calendar',
            '__timezone_database_version', '__dst_policy', '__fingerprint')

    def __init__(
            self,
            now: datetime.datetime,
            *,
            timezone: datetime.tzinfo | str | None = None,
            calendar: Calendar | None = None,
            timezone_database_version: str | None = None,
            dst_policy: DstResolutionPolicy | str = DstResolutionPolicy.RAISE
    ) -> None:
        if not isinstance(now, datetime.datetime):
            raise TypeError('now must be a datetime.datetime instance, got ' + type(now).__name__)
        policy = _as_dst_policy(dst_policy)
        if timezone is None:
            tzinfo = now.tzinfo
            if tzinfo is None:
                # bare naive instant: interpret in the local zone, matching the legacy $now default
                tzinfo = dateutil.tz.tzlocal()
        else:
            tzinfo, _ = _resolve_timezone(timezone)
        if now.tzinfo is None:
            # naive business instant: it is a wall time in the policy timezone, so apply DST policy
            now = self._localize(now, tzinfo, policy)
        # business timezone may differ from the instant's own zone (e.g. UTC instant + 'America/New_York'
        # policy zone); all day-boundary and calendar derivations go through timezone conversion
        self.__now = now
        self.__timezone = tzinfo
        self.__timezone_name = _tz_name(tzinfo)
        self.__calendar = calendar
        self.__timezone_database_version = timezone_database_version or detect_timezone_database_version()
        self.__dst_policy = policy
        parts = (
                self.__now.isoformat(),
                self.__timezone_name,
                self.__timezone_database_version,
                self.__calendar.fingerprint if self.__calendar is not None else '',
                self.__dst_policy.value,
        )
        self.__fingerprint = hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()

    # -- construction helpers ----------------------------------------------
    @classmethod
    def current(
            cls,
            *,
            timezone: datetime.tzinfo | str | None = None,
            calendar: Calendar | None = None,
            timezone_database_version: str | None = None,
            dst_policy: DstResolutionPolicy | str = DstResolutionPolicy.RAISE
    ) -> 'TemporalSnapshot':
        """以系统墙上时钟的当前时刻（UTC）构造快照。"""
        return cls(
                datetime.datetime.now(tz=datetime.timezone.utc),
                timezone=timezone,
                calendar=calendar,
                timezone_database_version=timezone_database_version,
                dst_policy=dst_policy
        )

    def replace(self, *, now: datetime.datetime | None = None, **kwargs: Any) -> 'TemporalSnapshot':
        """返回一份副本，替换业务时刻或其它字段（用于嵌套评估）。"""
        values = {
                'timezone': kwargs.pop('timezone', self.__timezone_name or self.__timezone),
                'calendar': kwargs.pop('calendar', self.__calendar),
                'timezone_database_version': kwargs.pop('timezone_database_version', self.__timezone_database_version),
                'dst_policy': kwargs.pop('dst_policy', self.__dst_policy),
        }
        if kwargs:
            raise TypeError('unexpected keyword argument(s): ' + ', '.join(sorted(kwargs)))
        return TemporalSnapshot(now if now is not None else self.__now, **values)

    # -- identity -----------------------------------------------------------
    @property
    def now(self) -> datetime.datetime:
        """
        业务时刻，以业务时区表达（始终时区感知）。

        返回的绝对时刻固定不变，但表示采用业务时区，因此规则中的
        ``$now.hour``、``$now.day`` 等墙上分量读到的是保单时区下的值，
        与历史 ``datetime.now(tz=timezone)`` 行为一致。需要绝对时刻时可自行
        转换到 UTC。
        """
        return self.__now.astimezone(self.__timezone)

    @property
    def timezone(self) -> datetime.tzinfo:
        return self.__timezone

    @property
    def timezone_name(self) -> str:
        """业务时区的规范名称（IANA 名称或 tzinfo 的类名）。"""
        return self.__timezone_name

    @property
    def timezone_database_version(self) -> str:
        return self.__timezone_database_version

    @property
    def calendar(self) -> Calendar | None:
        return self.__calendar

    @property
    def dst_policy(self) -> DstResolutionPolicy:
        return self.__dst_policy

    @property
    def fingerprint(self) -> str:
        """快照完整指纹：业务时刻、时区、tzdata 版本、日历版本与 DST 策略的哈希。"""
        return self.__fingerprint

    @property
    def versions(self) -> dict[str, str]:
        """人类可读的版本字典，可随业务决定落库/写入审计日志。"""
        return {
                'timezone_database': self.__timezone_database_version,
                'timezone': self.__timezone_name,
                'calendar': self.__calendar.version if self.__calendar is not None else 'none',
                'calendar_fingerprint': self.__calendar.fingerprint if self.__calendar is not None else 'none',
                'dst_policy': self.__dst_policy.value,
        }

    def compatible_with(self, other: 'TemporalSnapshot', *, require_calendar_content: bool = True) -> None:
        """
        确认两份快照使用相同的时态版本（时区、tzdata、日历、DST 策略）。

        业务时刻允许不同——重放历史决定时通常以历史时刻构造快照，但版本必须一致。
        任何不一致都抛出 :py:exc:`TemporalError`；无异常返回即确认通过。
        """
        mismatches: list[str] = []
        if self.__timezone_name != other.__timezone_name:
            mismatches.append('timezone: {0!r} != {1!r}'.format(self.__timezone_name, other.__timezone_name))
        if self.__timezone_database_version != other.__timezone_database_version:
            mismatches.append(
                    'timezone database: {0!r} != {1!r}'.format(self.__timezone_database_version, other.__timezone_database_version))
        if self.__dst_policy is not other.__dst_policy:
            mismatches.append('DST policy: {0!r} != {1!r}'.format(self.__dst_policy.value, other.__dst_policy.value))
        if (self.__calendar is None) != (other.__calendar is None):
            mismatches.append(
                    'calendar: {0!r} != {1!r}'.format(
                            self.__calendar and self.__calendar.version, other.__calendar and other.__calendar.version))
        elif self.__calendar is not None and other.__calendar is not None:
            if require_calendar_content:
                if self.__calendar != other.__calendar:
                    mismatches.append('calendar: {0!r} != {1!r} (content differs)'.format(
                            self.__calendar.fingerprint, other.__calendar.fingerprint))
            elif self.__calendar.version != other.__calendar.version:
                mismatches.append('calendar version: {0!r} != {1!r}'.format(
                        self.__calendar.version, other.__calendar.version))
        if mismatches:
            raise TemporalError('temporal snapshot is not compatible for replay: ' + '; '.join(mismatches))

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容的普通字典。"""
        return {
                'now': self.__now.isoformat(),
                'timezone': self.__timezone_name,
                'timezone_database_version': self.__timezone_database_version,
                'calendar': self.__calendar.to_dict() if self.__calendar is not None else None,
                'dst_policy': self.__dst_policy.value,
                'fingerprint': self.__fingerprint,
        }

    def to_json(self, **kwargs: Any) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> 'TemporalSnapshot':
        """
        从 :py:meth:`to_dict` 的结果重建快照，并校验指纹。

        时区按记录的 IANA 名称在**当前**时区数据库中重新解析；若当前环境缺少
        该时区，抛出 :py:exc:`TemporalError`。
        """
        try:
            snapshot = cls(
                    datetime.datetime.fromisoformat(value['now']),
                    timezone=value.get('timezone'),
                    calendar=Calendar.from_dict(value['calendar']) if value.get('calendar') else None,
                    timezone_database_version=value.get('timezone_database_version'),
                    dst_policy=value.get('dst_policy', DstResolutionPolicy.RAISE.value),
            )
        except KeyError as error:
            raise TemporalError('missing required snapshot field: {0!r}'.format(error.args[0])) from None
        recorded = value.get('fingerprint')
        if recorded is not None and recorded != snapshot.fingerprint:
            raise TemporalError(
                    'temporal snapshot fingerprint mismatch: the recorded inputs do not match their recorded digest '
                    '(corrupted or tampered record)')
        return snapshot

    @classmethod
    def from_json(cls, value: str) -> 'TemporalSnapshot':
        return cls.from_dict(json.loads(value))

    # -- time derivation (all rule temporal operations go through here) -----
    def local_time(self, value: datetime.datetime | None = None) -> datetime.datetime:
        """把时刻转换到业务时区；缺省返回业务时刻。"""
        value = self.__now if value is None else value
        return value.astimezone(self.__timezone)

    def start_of_day(self, value: datetime.datetime | None = None) -> datetime.datetime:
        """日期截断：返回业务时区下当日 00:00（保留时区感知）。"""
        local = self.local_time(value)
        return self._localize_datetime(datetime.datetime.combine(local.date(), datetime.time()))

    def today(self) -> datetime.datetime:
        """业务时刻在业务时区下的当日零点（供 ``$today`` 使用）。"""
        return self.start_of_day(self.__now)

    def _with_local_date(self, local: datetime.datetime, day: datetime.date) -> datetime.datetime:
        # rebuild the wall clock components in the business zone so DST policy applies (e.g. midnight or the
        # preserved time-of-day may be non-existent/ambiguous on the target date)
        naive = datetime.datetime.combine(day, local.timetz().replace(tzinfo=None))
        return self._localize_datetime(naive)

    def start_of(self, unit: str, value: datetime.datetime | None = None) -> datetime.datetime:
        """按单位（``day``/``week``/``month``/``year``）截断。周以周一为起点。"""
        local = self.local_time(value)
        unit = unit.lower()
        if unit == 'day':
            day = local.date()
        elif unit == 'week':
            day = (local - datetime.timedelta(days=local.weekday())).date()
        elif unit == 'month':
            day = local.date().replace(day=1)
        elif unit == 'year':
            day = local.date().replace(month=1, day=1)
        else:
            raise FunctionCallError("unsupported truncation unit: {0!r} (expected day, week, month or year)".format(unit))
        return self._localize_datetime(datetime.datetime.combine(day, datetime.time()))

    def window(self, duration: datetime.timedelta, *, anchor: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
        """
        返回半开区间 ``[start, end)``：``end`` 为锚点（缺省为业务时刻），
        ``start = end - duration``。两端均为时区感知的绝对时刻。
        """
        if not isinstance(duration, datetime.timedelta):
            raise FunctionCallError('window duration must be a timedelta value')
        end = self.__now if anchor is None else self.local_time(anchor)
        return (end - duration, end)

    def window_days(self, days: int, *, anchor: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
        """以业务日为单位的窗口（半开区间），按业务时区对齐到日界。"""
        if not isinstance(days, int):
            raise FunctionCallError('window day count must be an integer number')
        end_date = self.local_time(self.__now if anchor is None else anchor).date()
        start_date = end_date - datetime.timedelta(days=days)
        return (self._localize_datetime(datetime.datetime.combine(start_date, datetime.time())),
                self._localize_datetime(datetime.datetime.combine(end_date, datetime.time())))

    def within(self, value: datetime.datetime, start: datetime.datetime, end: datetime.datetime) -> bool:
        """*value* 是否落在半开区间 ``[start, end)``（按绝对时刻比较，正确处理跨日/跨时区）。"""
        value = self.local_time(value)
        start = self.local_time(start)
        end = self.local_time(end)
        if start > end:
            start, end = end, start
        return start <= value < end

    def add_business_days(self, value: datetime.datetime | None, count: int) -> datetime.datetime:
        """沿日历的工作日推进日期（保留原时刻的墙上时分秒）；未提供日历时抛出错误。"""
        if self.__calendar is None:
            raise FunctionCallError('business-day arithmetic requires a calendar on the temporal snapshot')
        local = self.local_time(self.__now if value is None else value)
        target = self.__calendar.add_business_days(local.date(), count)
        return self._with_local_date(local, target)

    def is_holiday(self, value: datetime.datetime | datetime.date | None = None) -> bool:
        if self.__calendar is None:
            raise FunctionCallError('holiday lookups require a calendar on the temporal snapshot')
        if value is None:
            day = self.local_time(self.__now).date()
        elif isinstance(value, datetime.datetime):
            day = self.local_time(value).date()
        else:
            day = value
        return self.__calendar.is_holiday(day)

    def is_business_day(self, value: datetime.datetime | datetime.date | None = None) -> bool:
        if self.__calendar is None:
            raise FunctionCallError('business-day lookups require a calendar on the temporal snapshot')
        if value is None:
            day = self.local_time(self.__now).date()
        elif isinstance(value, datetime.datetime):
            day = self.local_time(value).date()
        else:
            day = value
        return self.__calendar.is_business_day(day)

    # -- DST handling -------------------------------------------------------
    def localize(self, naive: datetime.datetime, policy: DstResolutionPolicy | str | None = None) -> datetime.datetime:
        """
        按业务时区本地化一个无时区的墙上时刻，并按策略处理夏令时间隙/重叠。
        """
        if naive.tzinfo is not None:
            return naive.astimezone(self.__timezone)
        return self._localize(naive, self.__timezone, self.__dst_policy if policy is None else _as_dst_policy(policy))

    def _localize_datetime(self, naive: datetime.datetime) -> datetime.datetime:
        return self._localize(naive, self.__timezone, self.__dst_policy)

    @staticmethod
    def _localize(
            naive: datetime.datetime,
            tzinfo: datetime.tzinfo,
            policy: DstResolutionPolicy,
            tz_name: str | None = None
    ) -> datetime.datetime:
        if naive.tzinfo is not None:
            return naive.astimezone(tzinfo)
        name = tz_name or _tz_name(tzinfo)
        if not dateutil.tz.datetime_exists(naive, tzinfo):
            # spring-forward gap: the wall time does not exist in this zone
            if policy.raises_on_gap:
                raise TemporalError(
                        'wall time {0} does not exist in timezone {1!r} (spring-forward gap)'.format(naive.isoformat(), name),
                        instant=naive,
                        timezone_name=name)
            if policy is DstResolutionPolicy.SHIFT_FORWARD:
                # dateutil resolve_imaginary moves the time forward by the gap size
                localized = dateutil.tz.resolve_imaginary(naive.replace(tzinfo=tzinfo))
            else:  # SHIFT_BACKWARD: pin to the last valid instant before the transition
                localized = _shift_backward_in_gap(naive, tzinfo)
            return localized
        if dateutil.tz.datetime_ambiguous(naive, tzinfo):
            # fall-back fold: the wall time happens twice
            if policy.raises_on_fold:
                raise TemporalError(
                        'wall time {0} is ambiguous in timezone {1!r} (fall-back overlap)'.format(naive.isoformat(), name),
                        instant=naive,
                        timezone_name=name)
            if policy is DstResolutionPolicy.SHIFT_BACKWARD:
                return naive.replace(tzinfo=tzinfo, fold=1)
        return naive.replace(tzinfo=tzinfo)

    def __repr__(self) -> str:
        return "<{0} now={1!r} timezone={2!r} tzdb={3!r} calendar={4!r} >".format(
                self.__class__.__name__,
                self.__now.isoformat(),
                self.__timezone_name,
                self.__timezone_database_version,
                self.__calendar)

# ---------------------------------------------------------------------------
# Snapshot stack (nested evaluation, thread-safe)
# ---------------------------------------------------------------------------
class _SnapshotStack(threading.local):
    def __init__(self) -> None:
        self._stack: list[TemporalSnapshot] = []

    def push(self, snapshot: TemporalSnapshot) -> None:
        self._stack.append(snapshot)

    def pop(self) -> TemporalSnapshot:
        return self._stack.pop()

    @property
    def current(self) -> TemporalSnapshot | None:
        return self._stack[-1] if self._stack else None

    def __len__(self) -> int:
        return len(self._stack)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _as_dst_policy(value: DstResolutionPolicy | str) -> DstResolutionPolicy:
    if isinstance(value, DstResolutionPolicy):
        return value
    try:
        return DstResolutionPolicy(value)
    except ValueError:
        raise ValueError('invalid DST resolution policy: {0!r} (expected one of {1})'.format(
                value, ', '.join(p.value for p in DstResolutionPolicy))) from None

def _tz_name(tzinfo: datetime.tzinfo) -> str:
    for attr in ('key', 'zone'):
        name = getattr(tzinfo, attr, None)
        if isinstance(name, str):
            return name
    # canonicalize dateutil's unnamed UTC/local singletons so snapshots round-trip through Context defaults
    if isinstance(tzinfo, dateutil.tz.tzutc):
        return 'utc'
    if isinstance(tzinfo, datetime.timezone) and tzinfo.utcoffset(datetime.datetime(2000, 1, 1)) == datetime.timedelta(0):
        # stdlib timezone.utc (and fixed-offset UTC zones) canonicalize to the same 'utc' name
        return 'utc'
    if isinstance(tzinfo, dateutil.tz.tzlocal):
        return 'local'
    # dateutil tzfile instances only carry their on-disk path (system zoneinfo or the pip tzdata package)
    filename = getattr(tzinfo, '_filename', None)
    if isinstance(filename, str):
        marker = '/zoneinfo/'
        if marker in filename:
            return filename.rsplit(marker, 1)[1]
        for root in ('/usr/share/zoneinfo/', '/usr/share/lib/zoneinfo/', '/etc/zoneinfo/'):
            if filename.startswith(root):
                return filename[len(root):]
    return type(tzinfo).__name__

def _resolve_timezone(value: datetime.tzinfo | str | None) -> tuple[datetime.tzinfo, str]:
    if value is None:
        tzinfo = dateutil.tz.tzlocal()
        return tzinfo, _tz_name(tzinfo)
    if isinstance(value, str):
        name = value
        if name == 'local':
            tzinfo = dateutil.tz.tzlocal()
        elif name == 'utc':
            tzinfo = dateutil.tz.tzutc()
        else:
            tzinfo = dateutil.tz.gettz(name)
            if tzinfo is None and name == 'tzlocal':
                tzinfo = dateutil.tz.tzlocal()
            elif tzinfo is None and name == 'tzutc':
                tzinfo = dateutil.tz.tzutc()
        if tzinfo is None:
            raise TemporalError('unknown timezone: {0!r}'.format(value), timezone_name=value)
        return tzinfo, name if name not in ('utc',) else 'utc'
    if isinstance(value, datetime.tzinfo):
        return value, _tz_name(value)
    raise TypeError('timezone must be a tzinfo instance or an IANA timezone name, got ' + type(value).__name__)

def _shift_backward_in_gap(naive: datetime.datetime, tzinfo: datetime.tzinfo) -> datetime.datetime:
    # Walk backwards minute by minute until a real wall time is found, then take the
    # final instant of the pre-transition offset.
    probe = naive
    for _ in range(24 * 60):
        probe -= datetime.timedelta(minutes=1)
        if dateutil.tz.datetime_exists(probe, tzinfo):
            localized = probe.replace(tzinfo=tzinfo)
            return localized
    raise TemporalError('could not resolve non-existent wall time near {0}'.format(naive.isoformat()), instant=naive)

def snapshot_temporal_function(
        function: Callable[..., Any],
        *,
        needs_snapshot: bool = True
) -> Callable[..., Any]:
    """
    把自定义时间函数包装为内置函数：调用时自动注入当前评估的时态快照。

    *needs_snapshot* 为 ``True`` 时，用户函数首参数为 :py:class:`TemporalSnapshot`；
    否则不注入，但函数仍只能在快照作用域内调用（用于声明“本函数读取业务时间”）。
    快照缺失时抛出 :py:exc:`FunctionCallError`，避免自定义函数悄悄退回墙上时钟。
    """
    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        snapshot = get_current_snapshot()
        if snapshot is None:
            raise FunctionCallError(
                    'temporal function {0!r} was called outside of a snapshot evaluation; pass a TemporalSnapshot '
                    'to Rule.evaluate to replay it deterministically'.format(getattr(function, '__name__', function)))
        if needs_snapshot:
            return function(snapshot, *args, **kwargs)
        return function(*args, **kwargs)
    wrapper.__temporal_function__ = True  # type: ignore[attr-defined]
    return wrapper

def temporal_function(function: Callable[..., Any] | None = None, *, needs_snapshot: bool = True) -> Callable[..., Any]:
    """
    声明自定义时间函数的装饰器。

    可直接使用 ``@temporal_function``（首参数自动注入当前
    :py:class:`TemporalSnapshot`），也可带参数
    ``@temporal_function(needs_snapshot=False)`` 仅声明时态语义而不注入快照。
    注册方式与普通内置函数相同，通过 Context 的自定义 builtins 传入。
    """
    if function is None:
        return functools.partial(snapshot_temporal_function, needs_snapshot=needs_snapshot)
    return snapshot_temporal_function(function, needs_snapshot=needs_snapshot)

# process-wide stack accessor shared by Context and builtins
_snapshot_stack = _SnapshotStack()

def push_snapshot(snapshot: TemporalSnapshot) -> None:
    """进入快照作用域（由 Context 在评估期间调用，支持嵌套）。"""
    _snapshot_stack.push(snapshot)

def pop_snapshot() -> None:
    _snapshot_stack.pop()

def get_current_snapshot() -> TemporalSnapshot | None:
    """当前线程评估作用域内的快照；未使用快照的规则返回 ``None``（零成本路径）。"""
    return _snapshot_stack.current
