#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/engine/context.py
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

import collections
import collections.abc
import contextlib
import dataclasses
import datetime
import decimal
import functools
import threading
import warnings
from typing import Any, Callable, Iterator, cast

from .. import ast  # noqa: F401 — must be imported before builtins to avoid a circular import
from .. import builtins
from .. import errors
from .. import types
from ..suggestions import suggest_symbol
from ..temporal import (
    DSTDisambiguation,
    BusinessCalendar,
    TemporalSnapshot,
    resolve_timezone as _resolve_temporal_timezone,
)
from ..types import DataType, _DataTypeDef

from ._attribute_resolver import _AttributeResolver

import dateutil.tz

def _tls_getter(thread_local: threading.local, key: str, _builtins: Any) -> Any:
    # a function stub to be used with functools.partial for retrieving thread-local values
    return getattr(thread_local.storage, key)

def _default_type_resolver(_: str) -> _DataTypeDef:
    return types.DataType.UNDEFINED

def resolve_attribute(thing: Any, name: str) -> Any:
    """项目内部接口说明。"""
    if not hasattr(thing, name):
        raise errors.SymbolResolutionError(name, thing=thing, suggestion=suggest_symbol(name, dir(thing)))
    return getattr(thing, name)

def resolve_item(thing: Any, name: str) -> Any:
    """项目内部接口说明。"""
    if not isinstance(thing, collections.abc.Mapping):
        raise errors.SymbolResolutionError(name, thing=thing)
    if name not in thing:
        raise errors.SymbolResolutionError(name, thing=thing, suggestion=suggest_symbol(name, thing.keys()))
    return thing[name]

def _type_resolver(type_map: dict[str, _DataTypeDef], name: str) -> _DataTypeDef:
    if name not in type_map:
        raise errors.SymbolResolutionError(name, suggestion=suggest_symbol(name, type_map.keys()))
    return type_map[name]

def type_resolver_from_dict(dictionary: collections.abc.Mapping[str, Any]) -> Callable[[str], _DataTypeDef]:
    """项目内部接口说明。"""
    type_map = {key: value if types.DataType.is_definition(value) else types.DataType.from_value(value) for key, value in dictionary.items()}
    return functools.partial(_type_resolver, type_map)

def _collect_object_types(definition: _DataTypeDef, type_map: dict[str, _DataTypeDef], seen: set[str]) -> None:
    if DataType.is_type(definition, DataType.OBJECT):
        if definition.name in seen:
            return
        seen.add(definition.name)
        # don't overwrite an entry that was set earlier (e.g. a top-level field whose name happens to match)
        type_map.setdefault(definition.name, definition)
        for attr_type in definition.attributes.values():
            _collect_object_types(attr_type, type_map, seen)
        return
    if DataType.is_type(definition, DataType.MAPPING):
        _collect_object_types(definition.key_type, type_map, seen)
        _collect_object_types(definition.value_type, type_map, seen)
        return
    if isinstance(definition, types._CollectionDataTypeDef):
        _collect_object_types(definition.value_type, type_map, seen)
        return
    if DataType.is_type(definition, DataType.NULLABLE):
        _collect_object_types(definition.inner_type, type_map, seen)
        return

def type_resolver_from_dataclass(cls: type, *, strict: bool = True) -> Callable[[str], _DataTypeDef]:
    """项目内部接口说明。"""
    if not dataclasses.is_dataclass(cls):
        raise TypeError('type_resolver_from_dataclass argument 1 must be a dataclass, not ' + type(cls).__name__)
    root = types.DataType.OBJECT.from_dataclass(cls.__name__, cls, strict=strict)
    type_map: dict[str, _DataTypeDef] = dict(root.attributes)
    _collect_object_types(root, type_map, set())
    return functools.partial(_type_resolver, type_map)

def type_resolver_from_sqlalchemy(cls: type, *, strict: bool = True) -> Callable[[str], _DataTypeDef]:
    """项目内部接口说明。"""
    if not hasattr(cls, '__mapper__'):
        raise TypeError(
                'type_resolver_from_sqlalchemy argument 1 must be a SQLAlchemy mapped class, not '
                + type(cls).__name__
        )
    root = types.DataType.OBJECT.from_sqlalchemy(cls.__name__, cls, strict=strict)
    type_map: dict[str, _DataTypeDef] = dict(root.attributes)
    _collect_object_types(root, type_map, set())
    return functools.partial(_type_resolver, type_map)

class _ThreadLocalStorage(object):
    """项目内部接口说明。"""
    __slots__ = ('assignment_scopes', 'regex_groups', 'temporal_frames', 'evaluation_active', 'last_snapshot', 'auto_snapshot')
    assignment_scopes: 'collections.deque[dict[str, ast.Assignment]]'
    regex_groups: tuple[str, ...] | None
    temporal_frames: list[TemporalSnapshot]
    evaluation_active: bool
    last_snapshot: TemporalSnapshot | None
    auto_snapshot: TemporalSnapshot | None
    def __init__(self) -> None:
        self.assignment_scopes = collections.deque()
        self.regex_groups = None
        # 显式快照帧（Context.temporal_snapshot 或 evaluate(snapshot=...)）；自动快照不压栈。
        self.temporal_frames = []
        self.evaluation_active = False
        self.last_snapshot = None
        self.auto_snapshot = None

class Context(object):
    """项目内部接口说明。"""
    def __init__(
                    self,
                    *,
                    regex_flags: int = 0,
                    resolver: Callable[[Any, str], Any] | None = None,
                    type_resolver: Callable[[str], _DataTypeDef] | collections.abc.Mapping[str, Any] | None = None,
                    default_timezone: str | datetime.tzinfo = 'local',
                    default_value: Any = errors.UNDEFINED,
                    decimal_context: decimal.Context | None = None,
                    mapping_attribute_lookup: bool = True,
                    calendar: BusinessCalendar | None = None,
                    disambiguation: str | DSTDisambiguation = DSTDisambiguation.EARLIER,
                    gap_policy: str | DSTDisambiguation = DSTDisambiguation.RAISE
    ) -> None:
        """项目内部接口说明。"""
        self.regex_flags = regex_flags
        """The *regex_flags* parameter from :py:meth:`~__init__`"""
        self.symbols: set[str] = set()
        """
        规则引用的符号集合，其中部分或全部符号需要在求值时解析。
        This attribute can be used after a rule is generated to ensure that all symbols are valid before it is
        evaluated.
        """
        if isinstance(default_timezone, str):
            tz_key = default_timezone.lower()
            if tz_key == 'local':
                default_timezone = dateutil.tz.tzlocal()
            elif tz_key == 'utc':
                default_timezone = dateutil.tz.tzutc()
            else:
                # IANA 名称（如 'America/New_York'）区分大小写，必须按原样走 zoneinfo，
                # 保证 DST 判定与时态快照一致。
                try:
                    default_timezone = _resolve_temporal_timezone(default_timezone)
                except ValueError:
                    raise ValueError('unsupported timezone: {0}'.format(default_timezone)) from None
        elif not isinstance(default_timezone, datetime.tzinfo):
            raise TypeError('invalid default_timezone type')
        self._thread_local = threading.local()
        self.default_timezone = cast(datetime.tzinfo, default_timezone)
        """The *default_timezone* parameter from :py:meth:`~__init__`"""
        self.calendar = calendar
        """默认业务日历（节假日表）；可被单次评估的快照覆盖。"""
        self.disambiguation = DSTDisambiguation(disambiguation)
        """自动建立快照时使用的夏令时重叠策略。"""
        self.gap_policy = DSTDisambiguation(gap_policy)
        """自动建立快照时使用的夏令时缺失策略。"""
        self.default_value = default_value
        """The *default_value* parameter from :py:meth:`~__init__`"""
        self.builtins = builtins.Builtins.from_defaults(
                values={'re_groups': builtins.BuiltinValueGenerator(functools.partial(_tls_getter, self._thread_local, 'regex_groups'))},
                value_types={'re_groups': types.DataType.ARRAY(types.DataType.STRING)},
                timezone=default_timezone,
                snapshot_provider=self._current_snapshot
        )
        """An instance of :py:class:`~rule_engine.builtins.Builtins` to provided a default set of builtin symbol values."""
        self.decimal_context = decimal_context or decimal.getcontext()
        """The *decimal_context* parameter from :py:meth:`~__init__`"""
        if isinstance(type_resolver, collections.abc.Mapping):
            type_resolver = type_resolver_from_dict(type_resolver)
        self.__type_resolver = type_resolver or _default_type_resolver
        self.__resolver = resolver or resolve_item
        self.mapping_attribute_lookup = mapping_attribute_lookup
        """The *mapping_attribute_lookup* parameter from :py:meth:`~__init__`."""
        self._mapping_fallback_lock = threading.Lock()
        self._mapping_fallback_warned = False

    def __getstate__(self) -> dict[str, Any]:
        return {
                'regex_flags': self.regex_flags,
                'symbols': self.symbols,
                'default_timezone': self.default_timezone,
                'default_value': self.default_value,
                'decimal_context': self.decimal_context,
                'mapping_attribute_lookup': self.mapping_attribute_lookup,
                'calendar': self.calendar,
                'disambiguation': self.disambiguation,
                'gap_policy': self.gap_policy,
                '_mapping_fallback_warned': self._mapping_fallback_warned,
                '_Context__type_resolver': self.__type_resolver,
                '_Context__resolver': self.__resolver,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.regex_flags = state['regex_flags']
        self.symbols = state['symbols']
        self.default_timezone = state['default_timezone']
        self.default_value = state['default_value']
        self.decimal_context = state['decimal_context']
        self.mapping_attribute_lookup = state['mapping_attribute_lookup']
        self.calendar = state.get('calendar')
        self.disambiguation = state.get('disambiguation', DSTDisambiguation.EARLIER)
        self.gap_policy = state.get('gap_policy', DSTDisambiguation.RAISE)
        self._mapping_fallback_warned = state['_mapping_fallback_warned']
        self.__type_resolver = state['_Context__type_resolver']
        self.__resolver = state['_Context__resolver']
        # recreate transient objects that can not be pickled
        self._thread_local = threading.local()
        self._mapping_fallback_lock = threading.Lock()
        self.builtins = builtins.Builtins.from_defaults(
                values={'re_groups': builtins.BuiltinValueGenerator(functools.partial(_tls_getter, self._thread_local, 'regex_groups'))},
                value_types={'re_groups': types.DataType.ARRAY(types.DataType.STRING)},
                timezone=self.default_timezone,
                snapshot_provider=self._current_snapshot
        )

    @contextlib.contextmanager
    def assignments(self, *assignments: 'ast.Assignment') -> Iterator[None]:
        """项目内部接口说明。"""
        self._tls.assignment_scopes.append({assign.name: assign for assign in assignments})
        try:
            yield
        finally:
            self._tls.assignment_scopes.pop()

    # ------------------------------------------------------------------
    # 时态快照（temporal snapshot）
    # ------------------------------------------------------------------
    def _new_auto_snapshot(self) -> TemporalSnapshot:
        return TemporalSnapshot.create(
                None,
                timezone=self.default_timezone,
                calendar=self.calendar,
                disambiguation=self.disambiguation,
                gap_policy=self.gap_policy,
        )

    def _current_snapshot(self) -> TemporalSnapshot | None:
        """供内置符号回调：返回当前线程生效的快照。

        优先使用显式快照栈（:meth:`temporal_snapshot` 或 ``evaluate(snapshot=...)``）；否则在评估
        进行中且规则实际引用了时间符号时，惰性建立一次自动快照。未引用任何时间符号的规则永远不会
        触发快照建立——不读时钟、不探测 tzdata 版本、也不发生压栈/弹栈开销。
        """
        if not hasattr(self._thread_local, 'storage'):
            return None
        tls = self._tls
        frames = tls.temporal_frames
        if frames:
            tls.last_snapshot = frames[-1]
            return frames[-1]
        if tls.evaluation_active:
            if tls.auto_snapshot is None:
                tls.auto_snapshot = self._new_auto_snapshot()
            tls.last_snapshot = tls.auto_snapshot
            return tls.auto_snapshot
        return None

    def current_temporal_snapshot(self) -> TemporalSnapshot | None:
        """非惰性地查看当前线程生效的快照（未引用时间符号时可能为 ``None``）。"""
        if not hasattr(self._thread_local, 'storage'):
            return None
        frames = self._tls.temporal_frames
        return frames[-1] if frames else None

    def last_temporal_snapshot(self) -> TemporalSnapshot | None:
        """返回当前线程上最近一次评估实际使用的快照（从未实例化过时为 ``None``）。

        与 :meth:`current_temporal_snapshot` 不同，评估结束、快照帧弹出后该记录仍保留，
        供调用方审计刚才那次决定使用的业务时刻与版本。
        """
        if not hasattr(self._thread_local, 'storage'):
            return None
        return self._tls.last_snapshot

    def active_temporal_snapshot(self) -> TemporalSnapshot | None:
        """返回本次评估已生效的快照，但不主动建立（供日期时间算术读取 DST 策略）。

        规则引用了 ``$now`` 等时间符号时自动快照已经建立，算术随之采用同一策略；规则只做
        ``datetime ± timedelta`` 而不引用时钟时返回 ``None``，算术走标准库原生路径，不读时钟。
        """
        if not hasattr(self._thread_local, 'storage'):
            return None
        tls = self._tls
        return tls.temporal_frames[-1] if tls.temporal_frames else tls.auto_snapshot

    @contextlib.contextmanager
    def temporal_snapshot(
            self,
            snapshot: TemporalSnapshot | None = None,
            *,
            moment: datetime.datetime | None = None,
            calendar: BusinessCalendar | None = None,
            timezone: str | datetime.tzinfo | None = None,
            disambiguation: str | DSTDisambiguation | None = None,
            gap_policy: str | DSTDisambiguation | None = None
    ) -> Iterator[TemporalSnapshot]:
        """为代码块（通常包含一次或多次 :meth:`Rule.evaluate`）建立时态快照。

        传入 *snapshot* 直接使用既有快照（重放场景）；否则按 *moment*（省略取当前时刻）等参数新建。
        快照压入线程局部栈：块内的评估（包括自定义函数中嵌套触发的评估）默认继承该快照；内层可用
        自己的快照覆盖，退出后自动恢复外层快照。

        .. code-block:: python

           with context.temporal_snapshot(moment=decision_time, calendar=cal) as snap:
               rule.matches(thing)          # $now/$today/节假日全部取自 snap
               print(snap.versions)         # 确认时区数据库与日历版本
        """
        if snapshot is None:
            snapshot = TemporalSnapshot.create(
                    moment,
                    timezone=self.default_timezone if timezone is None else timezone,
                    calendar=self.calendar if calendar is None else calendar,
                    disambiguation=self.disambiguation if disambiguation is None else disambiguation,
                    gap_policy=self.gap_policy if gap_policy is None else gap_policy,
            )
        else:
            if any(value is not None for value in (moment, calendar, timezone, disambiguation, gap_policy)):
                raise ValueError('snapshot can not be combined with moment/calendar/timezone/disambiguation/gap_policy')
        frames = self._tls.temporal_frames
        frames.append(snapshot)
        self._tls.last_snapshot = snapshot
        try:
            yield snapshot
        finally:
            frames.pop()

    @property
    def _tls(self) -> _ThreadLocalStorage:
        if not hasattr(self._thread_local, 'storage'):
            self._thread_local.storage = _ThreadLocalStorage()
            self._thread_local.storage.evaluation_active = False
        storage = self._thread_local.storage
        assert isinstance(storage, _ThreadLocalStorage)
        return storage

    def resolve(self, thing: Any, name: str, scope: str | None = None) -> Any:
        """项目内部接口说明。"""
        if scope == builtins.Builtins.scope_name:
            thing = self.builtins
        if isinstance(thing, builtins.Builtins):
            return resolve_item(thing, name)
        if scope is None:
            for assignments in self._tls.assignment_scopes:
                if name in assignments:
                    return assignments[name].value
            return self.__resolver(thing, name)
        raise errors.SymbolResolutionError(name, symbol_scope=scope, thing=thing)

    __resolve_attribute = _AttributeResolver()
    def resolve_attribute(self, thing: Any, object_: Any, name: str) -> Any:
        """项目内部接口说明。"""
        return self.__resolve_attribute(thing, object_, name)
    resolve_attribute_type = __resolve_attribute.resolve_type

    def _warn_mapping_fallback(self, attribute_name: str) -> None:
        with self._mapping_fallback_lock:
            if self._mapping_fallback_warned:
                return
            self._mapping_fallback_warned = True
        message = (
                "accessing attribute {0!r} on a MAPPING value via dot syntax is deprecated; "
                "use mapping[{0!r}] instead. This fallback will be removed in v6.0. Set "
                "Context(mapping_attribute_lookup=False) to opt out now, or filter "
                "rule_engine.errors.MappingAttributeLookupDeprecation to silence this warning."
        ).format(attribute_name)
        warnings.warn(errors.MappingAttributeLookupDeprecation(message), stacklevel=2)

    def resolve_type(self, name: str, scope: str | None = None) -> _DataTypeDef:
        """项目内部接口说明。"""
        if scope == builtins.Builtins.scope_name:
            return self.builtins.resolve_type(name)
        for assignments in self._tls.assignment_scopes:
            if name in assignments:
                value_type = assignments[name].value_type
                return value_type if value_type is not None else types.DataType.UNDEFINED
        return self.__type_resolver(name)
