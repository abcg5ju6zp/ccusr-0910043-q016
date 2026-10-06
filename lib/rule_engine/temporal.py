#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/temporal.py
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
#  LIMITED TO, THE IMPLIED WARRANTIES OF FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS
#  BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#

"""
时态快照（temporal snapshot）支持。

续保规则可能同时引用业务时刻、保单时区与节假日日历（例如"出险时间在续保前 30 天窗口内、且保单尚未
超过 60 天宽限期，宽限期只计工作日"）。若评估过程中直接读取系统时钟，则同一条规则对同一份输入，在
跨越分钟、跨日、时区数据库更新或节假日表修订后可能得到不同结果，历史决定无法重放。

本模块为*每次评估*提供一个不可变快照 :class:`TemporalSnapshot`，统一携带：

* **业务时刻**（``moment``）——规则内 ``$now`` / ``$today`` 及一切时间推导的唯一来源；
* **时区数据库版本**（``timezone_version``）——快照实际使用的 IANA tzdata 版本；
* **日历版本**（:class:`BusinessCalendar` / ``calendar_version``）——节假日表的不可变版本。

规则中的窗口、日期截断与持续时间运算一律从快照推导（见 :func:`start_of_day`、
:meth:`TemporalSnapshot.shift`、:meth:`TemporalSnapshot.window`、
:meth:`TemporalSnapshot.within_grace_period`），因此调用方可以：

1. 用历史业务时刻重放过去的决定（:meth:`TemporalSnapshot.frozen` 或
   ``Rule.evaluate(thing, snapshot=...)``）；
2. 通过 :attr:`TemporalSnapshot.versions` 确认决定实际使用的版本；
3. 嵌套评估时显式继承或覆盖快照（见 :class:`~rule_engine.engine.Context` 的
   :meth:`~rule_engine.engine.Context.temporal_snapshot` 上下文管理器）。

夏令时语义（以保单所在时区为准，例如 America/New_York）：

* **重叠时刻**（秋季回拨，本地墙上时间出现两次）：默认取较早的一次（``fold=0``，夏令时），
  可通过 ``disambiguation='later'`` 取较晚一次，或 ``'raise'`` 直接报错；
* **缺失时刻**（春季前拨，本地墙上时间不存在）：默认 ``gap_policy='raise'`` 抛出
  :class:`TemporalAmbiguityError`，也可选择 ``'forward'``（取 gap 之后）或
  ``'backward'``（取 gap 之前）。

持续时间语义（同一个 :class:`datetime.timedelta` 有两种解释，调用方按需选择）：

* 规则语言中的 ``datetime ± t'…'`` 走 :func:`shift_wall_clock`：保持本地墙上钟点的*日历式*加法
  （``P1D`` = "明天同一钟点"），仅当结果恰好落在 gap/重叠时才按快照策略重新解释；
* :meth:`TemporalSnapshot.shift` 是*固定物理时长*：先在 UTC 瞬时上加减（``P1D`` 恒为 24 小时，
  跨 DST 钟点可能改变），再投影回业务时区，结果永远是合法瞬时；
* "日历日 / 日历月 / 工作日"等日历单位由 :meth:`TemporalSnapshot.add_calendar_days`、
  :meth:`TemporalSnapshot.add_calendar_months`、:meth:`TemporalSnapshot.add_business_days` 与
  :class:`BusinessCalendar` 解释，节假日表随日历版本冻结。
"""

from __future__ import annotations

import datetime
import enum
import functools
import importlib.metadata
import os
import types
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Any, FrozenSet, Iterable, Mapping

import dateutil.tz

from . import errors

__all__ = (
    'BusinessCalendar',
    'DSTDisambiguation',
    'TEMPORAL_BUILTIN_NAMES',
    'TemporalAmbiguityError',
    'TemporalSnapshot',
    'TemporalVersionMismatchError',
    'detect_timezone_version',
    'localize',
    'resolve_timezone',
    'start_of_day',
)

#: 解析与求值依赖当前评估快照的内置符号名（重放时随业务时刻变化）。
TEMPORAL_BUILTIN_NAMES = frozenset({
        'now',
        'today',
        'start_of_day',
        'add_days',
        'add_months',
        'add_business_days',
        'is_holiday',
        'is_business_day',
})

_UTC = datetime.timezone.utc


class DSTDisambiguation(str, enum.Enum):
    """夏令时歧义时刻的解析策略。"""
    EARLIER = 'earlier'
    """重叠时刻取较早的一次（``fold=0``，默认）。"""
    LATER = 'later'
    """重叠时刻取较晚的一次（``fold=1``）。"""
    FORWARD = 'forward'
    """缺失时刻向前平移到 gap 结束之后。"""
    BACKWARD = 'backward'
    """缺失时刻向后平移到 gap 开始之前。"""
    RAISE = 'raise'
    """歧义时刻抛出 :class:`TemporalAmbiguityError`。"""


class TemporalAmbiguityError(errors.EvaluationError):
    """本地墙上时间在目标时区中不明确（落入夏令时重叠或缺失区间）时抛出。"""
    def __init__(self, message: str, *, wall_time: datetime.datetime | None = None) -> None:
        super().__init__(message)
        self.wall_time = wall_time
        """触发歧义的、带时区的本地墙上时间（若适用）。"""


class TemporalVersionMismatchError(errors.EvaluationError):
    """重放快照声明的版本与运行环境实际版本不一致时抛出（需显式启用版本校验）。"""
    def __init__(self, kind: str, expected: str, actual: str) -> None:
        super().__init__("temporal version mismatch for {0}: expected {1!r} but environment provides {2!r}".format(
                kind, expected, actual))
        self.kind = kind
        self.expected = expected
        self.actual = actual


@functools.lru_cache(maxsize=None)
def _zoneinfo_cached(key: str) -> datetime.tzinfo:
    # 集中缓存 ZoneInfo 构造；解析失败给出明确错误。
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python >= 3.9 始终提供 zoneinfo
        raise RuntimeError('zoneinfo is required to resolve named timezones') from None
    try:
        return ZoneInfo(key)
    except Exception:
        raise ValueError('unknown timezone: ' + key) from None


def resolve_timezone(tz: str | datetime.tzinfo) -> datetime.tzinfo:
    """把 ``'local'`` / ``'utc'`` / IANA 名称解析为具体的 :class:`datetime.tzinfo`。"""
    if isinstance(tz, datetime.tzinfo):
        return tz
    if tz.lower() == 'local':
        return dateutil.tz.tzlocal()
    if tz.lower() == 'utc':
        return dateutil.tz.tzutc()
    # IANA 名称区分大小写（America/New_York），按原样传给 ZoneInfo。
    return _zoneinfo_cached(tz)


@functools.lru_cache(maxsize=None)
def detect_timezone_version() -> str:
    """探测当前生效的 IANA 时区数据库版本。

    依次检查系统 ``+VERSION`` 文件与 ``tzdata`` 发行包。探测不到时返回 ``'unknown'``，
    但永远不会抛出——版本不可知不应阻断评估。
    """
    try:
        from zoneinfo import TZPATH
    except ImportError:  # pragma: no cover
        TZPATH = ()  # type: ignore[assignment]
    for directory in TZPATH:
        try:
            with open(os.path.join(directory, '+VERSION'), 'r', encoding='ascii') as file_handle:
                version = file_handle.read().strip()
            if version:
                return version
        except OSError:
            continue
    try:
        return importlib.metadata.version('tzdata')
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def localize(
        wall_time: datetime.datetime | datetime.date,
        timezone: datetime.tzinfo,
        *,
        disambiguation: DSTDisambiguation = DSTDisambiguation.EARLIER,
        gap_policy: DSTDisambiguation = DSTDisambiguation.RAISE
) -> datetime.datetime:
    """把朴素本地墙上时间附加到 *timezone*，按显式策略处理重叠与缺失。

    返回一个保证与目标时区一致的感知时刻（``gap_policy='raise'`` 且落入 gap 时抛出异常除外）。
    """
    if isinstance(wall_time, datetime.datetime):
        naive = wall_time.replace(tzinfo=None)
    elif isinstance(wall_time, datetime.date):
        naive = datetime.datetime.combine(wall_time, datetime.time())
    else:
        raise TypeError('wall_time must be a datetime or date')
    # 用 fold=0 / fold=1 各附加一次；dateutil 的判定同时兼容 zoneinfo 与 dateutil 自身时区。
    first = naive.replace(tzinfo=timezone, fold=0)
    second = naive.replace(tzinfo=timezone, fold=1)
    if not dateutil.tz.datetime_exists(first):
        # 缺失时刻（spring-forward gap）
        if gap_policy == DSTDisambiguation.FORWARD:
            return second
        if gap_policy == DSTDisambiguation.BACKWARD:
            return first
        raise TemporalAmbiguityError(
                "local time {0!r} does not exist in timezone {1!r} (spring-forward gap)".format(naive, timezone),
                wall_time=first
        )
    if dateutil.tz.datetime_ambiguous(first):
        # 重叠时刻（fall-back overlap）
        if disambiguation == DSTDisambiguation.LATER:
            return second
        if disambiguation == DSTDisambiguation.RAISE:
            raise TemporalAmbiguityError(
                    "local time {0!r} is ambiguous in timezone {1!r} (fall-back overlap)".format(naive, timezone),
                    wall_time=first
            )
        return first
    return first


def start_of_day(
        moment: datetime.datetime,
        *,
        timezone: datetime.tzinfo | None = None,
        disambiguation: DSTDisambiguation = DSTDisambiguation.EARLIER,
        gap_policy: DSTDisambiguation = DSTDisambiguation.FORWARD
) -> datetime.datetime:
    """把时刻截断到其所在时区日历日的 00:00:00（日期截断的唯一入口）。

    *moment* 必须是感知时刻。在极少数"午夜发生跳变"的时区（如 America/Sao_Paulo 的部分历史年份），
    该日的 00:00 是缺失时刻：截断端点是派生出的*区间边界*而非调用方指定的墙上时间，其语义固定为
    "该日历日的第一个有效瞬间"（``gap_policy`` 默认 ``FORWARD``，例如跳到 01:00）。
    """
    if moment.tzinfo is None:
        raise ValueError('start_of_day requires a timezone-aware moment')
    tz = timezone or moment.tzinfo
    return localize(moment.astimezone(tz).date(), tz, disambiguation=disambiguation, gap_policy=gap_policy)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


def shift_wall_clock(
        base: datetime.datetime,
        duration: datetime.timedelta,
        *,
        disambiguation: DSTDisambiguation = DSTDisambiguation.EARLIER,
        gap_policy: DSTDisambiguation = DSTDisambiguation.RAISE
) -> datetime.datetime:
    """日历式日期时间算术：``base ± duration`` 保持本地墙上钟点，但对 DST 边界给出明确语义。

    Python 原生 ``datetime + timedelta`` 对感知时刻做朴素墙上时间加法：当结果恰好落在春季缺失时刻时
    会静默返回一个错误瞬时，落在秋季重叠时刻时则沿用 ``fold``。本函数：

    * *base* 为朴素时刻时（无 DST 概念）直接返回原生结果；
    * 结果是唯一合法瞬时（绝大多数情况）时直接返回，不做额外工作；
    * 结果落入 gap / overlap 时，按 *disambiguation* / *gap_policy* 重新解释。

    与 :meth:`TemporalSnapshot.shift` 的区别：后者在 UTC 瞬时上加*固定物理时长*（``P1D`` 恒为 24
    小时，钟点可能改变）；本函数保持钟点（日历日语义，``P1D`` 永远是"明天同一钟点"）。
    """
    if base.tzinfo is None:
        return base + duration
    result = base + duration
    if dateutil.tz.datetime_exists(result) and not dateutil.tz.datetime_ambiguous(result):
        return result
    return localize(result.replace(tzinfo=None), base.tzinfo, disambiguation=disambiguation, gap_policy=gap_policy)


class BusinessCalendar(object):
    """不可变的业务日历：节假日集合 + 每周休息日 + 日历版本标识。

    :param holidays: 节假日日期集合（按目标时区的本地日历日解释）。
    :param weekends: 每周休息日的星期序号（0=周一 … 6=周日），默认周六、周日。
    :param version: 日历版本标识；为 ``None`` 时由节假日内容派生稳定指纹（内容变更必然改变版本）。
        生产环境重放历史决定时应传入发布系统中的显式版本号。
    """
    __slots__ = ('_holidays', '_weekends', 'version')
    _holidays: FrozenSet[datetime.date]
    _weekends: FrozenSet[int]
    version: str
    """日历版本标识（显式传入或由节假日内容派生的指纹）。"""

    def __init__(
            self,
            holidays: Iterable[datetime.date] = (),
            *,
            weekends: Iterable[int] = (5, 6),
            version: str | None = None
    ) -> None:
        object.__setattr__(self, '_holidays', frozenset(holidays))
        for holiday in self._holidays:
            if not isinstance(holiday, datetime.date) or isinstance(holiday, datetime.datetime):
                raise TypeError('holidays must contain datetime.date instances, not ' + type(holiday).__name__)
        weekend_set = frozenset(weekends)
        if not all(isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6 for day in weekend_set):
            raise ValueError('weekends must be weekday numbers in the range 0..6 (0=Monday)')
        object.__setattr__(self, '_weekends', weekend_set)
        object.__setattr__(self, 'version', version or self._fingerprint())

    def __setattr__(self, name: str, value: Any) -> None:
        # 日历与其版本必须冻结：已发布日历在评估期间被修改会使历史决定无法重放。
        raise AttributeError("can't set attribute {!r}: BusinessCalendar is immutable".format(name))

    def __delattr__(self, name: str) -> None:
        raise AttributeError("can't delete attribute {!r}: BusinessCalendar is immutable".format(name))

    def __getstate__(self) -> tuple[Any, ...]:
        return (self._holidays, self._weekends, self.version)

    def __setstate__(self, state: tuple[Any, ...]) -> None:
        holidays, weekends, version = state
        object.__setattr__(self, '_holidays', holidays)
        object.__setattr__(self, '_weekends', weekends)
        object.__setattr__(self, 'version', version)

    def __repr__(self) -> str:
        return "<{} version={!r} holidays={} weekends={!r} >".format(
                self.__class__.__name__, self.version, len(self._holidays), tuple(sorted(self._weekends)))

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, BusinessCalendar):
            return NotImplemented
        return self._holidays == other._holidays and self._weekends == other._weekends and self.version == other.version

    def __hash__(self) -> int:
        return hash((self._holidays, self._weekends, self.version))

    @property
    def holidays(self) -> FrozenSet[datetime.date]:
        """节假日日期的不可变集合。"""
        return self._holidays

    @property
    def weekends(self) -> FrozenSet[int]:
        """每周休息日星期序号的不可变集合。"""
        return self._weekends

    def _fingerprint(self) -> str:
        # 内容指纹：节假日数 + 首尾日期 + 全部日期的稳定散列，足以检测意外漂移；
        # 生产重放仍建议显式传入发布版本号。
        ordered = tuple(sorted(self._holidays))
        if not ordered:
            return 'empty-v1'
        checksum = 0
        for day in ordered:
            checksum = (checksum * 31 + day.toordinal()) & 0xFFFFFFFF
        return 'cal-v1-{0:d}-{1!s}-{2!s}-{3:08x}'.format(len(ordered), ordered[0], ordered[-1], checksum)

    def is_business_day(self, day: datetime.date) -> bool:
        """*day* 是否为工作日（既非周末也非节假日）。"""
        return day.weekday() not in self._weekends and day not in self._holidays

    def is_holiday(self, day: datetime.date) -> bool:
        """*day* 是否为节假日（只查节假日表，周末不计入）。"""
        return day in self._holidays

    def is_non_business_day(self, day: datetime.date) -> bool:
        """*day* 是否为非工作日（周末或节假日）。"""
        return not self.is_business_day(day)

    def shift_business_days(self, day: datetime.date, count: int, *, limit: int = 100000) -> datetime.date:
        """把日历日向前（*count* 为负）或向后移动 ``abs(count)`` 个工作日。

        *count* 为 0 且当天是非工作日时，回退到最近的工作日。
        """
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError('count must be an integer')
        current = day
        if count == 0:
            while not self.is_business_day(current):
                current = current - datetime.timedelta(days=1)
            return current
        step = 1 if count > 0 else -1
        remaining = abs(count)
        guard = 0
        while remaining > 0:
            current = current + datetime.timedelta(days=step)
            if self.is_business_day(current):
                remaining -= 1
            guard += 1
            if guard > limit:  # pragma: no cover - 仅在日历全部是休息日时触发
                raise RuntimeError('business day shift exceeded safety limit')
        return current


@dataclass(frozen=True)
class TemporalSnapshot(object):
    """一次规则评估的不可变时态快照。

    所有时间推导都从 :attr:`moment` 出发；当日起点在构造时一次性预计算并冻结，因此同一次评估内
    ``$today`` 被引用多次也不会重复截断，且 DST 配置错误会在快照建立时立即暴露。
    """
    moment: datetime.datetime
    """业务时刻（必须是时区感知时刻），``$now`` 的唯一来源。"""
    timezone: datetime.tzinfo
    """保单/业务时区，日期截断与本地墙上时间解释均在此时区进行。"""
    calendar: BusinessCalendar | None = None
    """节假日日历；为 ``None`` 时工作日/节假日推导不可用。"""
    timezone_version: str | None = None
    """IANA 时区数据库版本；为 ``None`` 时在构造时自动探测并冻结。"""
    calendar_version: str | None = None
    """日历版本；为 ``None`` 时取 :attr:`calendar` 的版本（无日历则为 ``None``）。"""
    disambiguation: DSTDisambiguation = DSTDisambiguation.EARLIER
    """重叠时刻策略。"""
    gap_policy: DSTDisambiguation = DSTDisambiguation.RAISE
    """缺失时刻策略。"""
    verify_versions: bool = False
    """为真时校验显式声明的版本与运行环境一致（重放历史决定时使用）。"""
    _today: datetime.datetime | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.moment.tzinfo is None:
            raise ValueError('TemporalSnapshot.moment must be a timezone-aware datetime')
        if not isinstance(self.timezone, datetime.tzinfo):
            raise TypeError('TemporalSnapshot.timezone must be a datetime.tzinfo')
        if self.gap_policy not in (DSTDisambiguation.RAISE, DSTDisambiguation.FORWARD, DSTDisambiguation.BACKWARD):
            raise ValueError("gap_policy must be one of: 'raise', 'forward', 'backward'")
        if self.timezone_version is None:
            object.__setattr__(self, 'timezone_version', detect_timezone_version())
        elif self.verify_versions:
            self._verify_version('timezone database', self.timezone_version, detect_timezone_version())
        if self.calendar is not None and self.calendar_version is None:
            object.__setattr__(self, 'calendar_version', self.calendar.version)
        elif self.calendar is None and self.calendar_version is not None and self.verify_versions:
            raise TemporalVersionMismatchError('calendar', self.calendar_version, 'none')
        if self.calendar is not None and self.calendar_version is not None and self.verify_versions:
            self._verify_version('calendar', self.calendar_version, self.calendar.version)
        # 预截断一次：使 DST 策略错误在快照建立时即暴露，而不是延迟到规则求值中途。
        object.__setattr__(self, '_today', self._compute_today())

    @staticmethod
    def _verify_version(kind: str, expected: str, actual: str) -> None:
        if actual != 'unknown' and expected != actual:
            raise TemporalVersionMismatchError(kind, expected, actual)

    def _compute_today(self) -> datetime.datetime:
        # 截断端点是派生边界，午夜 gap 固定解释为"该日第一个有效瞬间"，不受显式时刻的 gap_policy 影响。
        return self._localize_day_boundary(self.moment.astimezone(self.timezone).date())

    def _localize_day_boundary(self, day: datetime.date) -> datetime.datetime:
        return localize(
                day,
                self.timezone,
                disambiguation=self.disambiguation,
                gap_policy=DSTDisambiguation.FORWARD,
        )

    @classmethod
    def create(
            cls,
            moment: datetime.datetime | None = None,
            *,
            timezone: str | datetime.tzinfo = 'local',
            calendar: BusinessCalendar | None = None,
            timezone_version: str | None = None,
            calendar_version: str | None = None,
            disambiguation: str | DSTDisambiguation = DSTDisambiguation.EARLIER,
            gap_policy: str | DSTDisambiguation = DSTDisambiguation.RAISE,
            verify_versions: bool = False
    ) -> 'TemporalSnapshot':
        """便捷构造：*moment* 省略时取系统当前时刻；*timezone* 可传 IANA 名称。"""
        tz = resolve_timezone(timezone)
        if moment is None:
            moment = datetime.datetime.now(tz=tz)
        elif moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        else:
            moment = moment.astimezone(tz)
        return cls(
                moment=moment,
                timezone=tz,
                calendar=calendar,
                timezone_version=timezone_version,
                calendar_version=calendar_version,
                disambiguation=DSTDisambiguation(disambiguation),
                gap_policy=DSTDisambiguation(gap_policy),
                verify_versions=verify_versions,
        )

    @classmethod
    def frozen(
            cls,
            moment: datetime.datetime | str,
            timezone: str | datetime.tzinfo = 'local',
            *,
            calendar: BusinessCalendar | None = None,
            timezone_version: str | None = None,
            calendar_version: str | None = None,
            verify_versions: bool = False,
            **kwargs: Any
    ) -> 'TemporalSnapshot':
        """以显式历史业务时刻构造快照，用于重放过去的决定。

        *moment* 也可传 ISO-8601 字符串。建议同时传入 *timezone_version* / *calendar_version* 并置
        *verify_versions* 为真，使历史决定与当前环境的版本差异立即可见。
        """
        if isinstance(moment, str):
            import dateutil.parser
            parsed = dateutil.parser.isoparse(moment)
        else:
            parsed = moment
        return cls.create(
                parsed,
                timezone=timezone,
                calendar=calendar,
                timezone_version=timezone_version,
                calendar_version=calendar_version,
                verify_versions=verify_versions,
                **kwargs
        )

    @property
    def now(self) -> datetime.datetime:
        """业务时刻（规则中 ``$now`` 的取值）。"""
        return self.moment

    @property
    def today(self) -> datetime.datetime:
        """业务时刻在业务时区下所在日历日的 00:00（规则中 ``$today`` 的取值）。"""
        assert self._today is not None
        return self._today

    @property
    def today_date(self) -> datetime.date:
        """业务时刻在业务时区下的本地日历日。"""
        return self.moment.astimezone(self.timezone).date()

    @property
    def versions(self) -> Mapping[str, str | None]:
        """本次评估实际使用的版本，供审计与持久化（只读映射）。"""
        return types.MappingProxyType({'timezone': self.timezone_version, 'calendar': self.calendar_version})

    def localize(self, wall_time: datetime.datetime | datetime.date) -> datetime.datetime:
        """按本快照的 DST 策略把本地墙上时间附加到业务时区。"""
        return localize(wall_time, self.timezone, disambiguation=self.disambiguation, gap_policy=self.gap_policy)

    def start_of_day(self, moment: datetime.datetime | None = None) -> datetime.datetime:
        """日期截断：*moment* 省略时等价于 :attr:`today`。

        午夜 gap 固定解释为该日第一个有效瞬间（与 :attr:`today` 一致）；显式时刻的 *gap_policy*
        只影响 :meth:`localize` 与持续时间算术，不影响日期截断边界。
        """
        if moment is None:
            return self.today
        target = self._as_aware(moment)
        return self._localize_day_boundary(target.astimezone(self.timezone).date())

    def shift(self, duration: datetime.timedelta, *, moment: datetime.datetime | None = None) -> datetime.datetime:
        """固定物理时长位移（持续时间推导的统一入口）。

        先在 UTC 瞬时上做精确加法，再投影回业务时区：跨日、跨 DST 均按真实经过时间计算，
        结果永远是合法瞬时，不会落在 gap / overlap 上。
        """
        base = self.moment if moment is None else self._as_aware(moment)
        return (base.astimezone(_UTC) + duration).astimezone(self.timezone)

    def add_calendar_days(self, days: int, *, moment: datetime.datetime | None = None) -> datetime.datetime:
        """按本地日历日位移（保持本地墙上钟点）。结果可能落在 DST 边界，按快照策略解析。"""
        base = self.moment if moment is None else self._as_aware(moment)
        local = base.astimezone(self.timezone)
        target_date = local.date() + datetime.timedelta(days=days)
        return self.localize(datetime.datetime.combine(target_date, local.timetz().replace(tzinfo=None)))

    def add_calendar_months(self, months: int, *, moment: datetime.datetime | None = None) -> datetime.datetime:
        """按日历月位移，保持本月内的日与钟点；目标月份没有该日时取该月最后一天。"""
        base = self.moment if moment is None else self._as_aware(moment)
        local = base.astimezone(self.timezone)
        year, month = _add_months(local.year, local.month, months)
        day = local.day
        while True:
            try:
                target = datetime.datetime(year, month, day, local.hour, local.minute, local.second, local.microsecond)
                break
            except ValueError:
                day -= 1
                if day <= 0:  # pragma: no cover - 不可能发生
                    raise
        return self.localize(target)

    def add_business_days(self, count: int, *, moment: datetime.datetime | None = None) -> datetime.datetime:
        """按工作日位移（跳过周末与节假日），保持本地钟点。需要 :attr:`calendar`。"""
        calendar = self._require_calendar()
        base = self.moment if moment is None else self._as_aware(moment)
        local = base.astimezone(self.timezone)
        target_date = calendar.shift_business_days(local.date(), count)
        return self.localize(datetime.datetime.combine(target_date, local.timetz().replace(tzinfo=None)))

    def window(
            self,
            duration: datetime.timedelta,
            *,
            anchor: datetime.datetime | None = None,
            calendar_days: int | None = None
    ) -> tuple[datetime.datetime, datetime.datetime]:
        """返回半开区间 ``[start, end)``，调用方以 ``start <= t < end`` 判定归属。

        * ``window(duration)``：以业务时刻为右端点，向过去取固定时长（续保"最近 N 天"窗口）；
        * ``window(duration, anchor=t0)``：以 *t0* 为左端点向右；
        * ``window(timedelta(), calendar_days=n)``：从业务时刻当日 00:00（或 *anchor* 当日）起的
          *n* 个本地日历日（跨日区间），右端点同样经过 DST 安全的本地时间构造。
        """
        if calendar_days is not None:
            if not isinstance(calendar_days, int) or isinstance(calendar_days, bool):
                raise TypeError('calendar_days must be an integer')
            start = self.today if anchor is None else self.start_of_day(anchor)
            end_day = start.astimezone(self.timezone).date() + datetime.timedelta(days=calendar_days)
            end = self._localize_day_boundary(end_day)
            return start, end
        if anchor is None:
            end = self.moment
            start = self.shift(-duration, moment=end)
        else:
            start = self._as_aware(anchor)
            end = self.shift(duration, moment=start)
        return start, end

    def is_holiday(self, moment: datetime.datetime | None = None) -> bool:
        """时刻（默认业务时刻）在业务时区下的日历日是否为节假日。"""
        calendar = self._require_calendar()
        target = self.moment if moment is None else self._as_aware(moment)
        return calendar.is_holiday(target.astimezone(self.timezone).date())

    def is_business_day(self, moment: datetime.datetime | None = None) -> bool:
        """时刻（默认业务时刻）在业务时区下是否为工作日。"""
        calendar = self._require_calendar()
        target = self.moment if moment is None else self._as_aware(moment)
        return calendar.is_business_day(target.astimezone(self.timezone).date())

    def within_grace_period(
            self,
            deadline: datetime.datetime,
            grace: datetime.timedelta,
            *,
            business_days: bool = False
    ) -> bool:
        """续保宽限期判定：业务时刻是否不晚于 *deadline* + *grace*（端点含等号，最后一刻仍有效）。

        *business_days* 为真时，*grace* 的天数部分按工作日推进（跳过周末与节假日），不足一天的
        余量仍按固定物理时长加在截止日本地钟点上；否则 *grace* 整体是固定物理时长。
        """
        deadline = self._as_aware(deadline)
        if business_days:
            calendar = self._require_calendar()
            local = deadline.astimezone(self.timezone)
            remainder = datetime.timedelta(seconds=grace.seconds, microseconds=grace.microseconds)
            end_date = calendar.shift_business_days(local.date(), grace.days)
            end = self.localize(datetime.datetime.combine(end_date, local.timetz().replace(tzinfo=None)))
            end = self.shift(remainder, moment=end)
        else:
            end = self.shift(grace, moment=deadline)
        return self.moment <= end

    def replace(self, **changes: Any) -> 'TemporalSnapshot':
        """返回替换了部分字段的新快照（嵌套评估覆盖业务时刻/日历时使用）。"""
        return dataclass_replace(self, **changes)

    def _as_aware(self, moment: datetime.datetime) -> datetime.datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.timezone)
        return moment.astimezone(self.timezone)

    def _require_calendar(self) -> BusinessCalendar:
        if self.calendar is None:
            raise errors.EvaluationError('temporal snapshot has no business calendar configured')
        return self.calendar
