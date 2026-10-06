#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/builtins.py
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
import datetime
import decimal
import functools
import math
import random
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import errors
from . import types
from .parser.utilities import parse_datetime, parse_float, parse_timedelta
from .temporal import TemporalSnapshot

import dateutil.tz

def _builtin_filter(function: Callable[[Any], Any], iterable: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(filter(function, iterable))

def _builtin_map(function: Callable[[Any], Any], iterable: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(map(function, iterable))

def _builtin_parse_datetime(builtins: 'Builtins', string: str) -> datetime.datetime:
    # 朴素时间戳一律按*本次评估快照*的业务时区解释；快照不可用时（直接使用 Builtins）回退到内置时区。
    snapshot = builtins.get_snapshot()
    timezone = snapshot.timezone if snapshot is not None else builtins.timezone
    return parse_datetime(string, timezone)

def _builtin_random(boundary: Any = None) -> Any:
    if boundary is not None:
        if not types.is_natural_number(boundary):
            raise errors.FunctionCallError('argument #1 (boundary) must be a natural number')
        return random.randint(0, int(boundary))
    return random.random()

def _builtin_now(builtins: 'Builtins') -> datetime.datetime:
    # $now 的唯一时钟来源是本次评估的时态快照；无快照（直接使用 Builtins）时才读取系统时钟。
    snapshot = builtins.get_snapshot()
    if snapshot is not None:
        return snapshot.now
    return datetime.datetime.now(tz=builtins.timezone)

def _builtin_today(builtins: 'Builtins') -> datetime.datetime:
    snapshot = builtins.get_snapshot()
    if snapshot is not None:
        return snapshot.today
    return _builtin_now(builtins).replace(hour=0, minute=0, second=0, microsecond=0)

def _builtin_parse_datetime_generator(builtins: 'Builtins') -> 'functools.partial[datetime.datetime]':
    return functools.partial(_builtin_parse_datetime, builtins)

def _assert_datetime_argument(value: Any, position: int) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise errors.FunctionCallError('argument #{} must be a datetime value'.format(position))
    return value

def _assert_integer_argument(value: Any, position: int, name: str) -> int:
    if not types.is_integer_number(value):
        raise errors.FunctionCallError('argument #{} ({}) must be an integer number'.format(position, name))
    return int(value)

def _snapshot_bound(generator_name: str, snapshot: TemporalSnapshot | None) -> TemporalSnapshot:
    if snapshot is None:
        raise errors.FunctionCallError(
                "{} requires a temporal snapshot; evaluate the rule with Rule.evaluate(thing, moment=...) "
                "or inside a Context.temporal_snapshot() block".format(generator_name)
        )
    return snapshot

def _builtin_start_of_day(builtins: 'Builtins') -> Callable[..., Any]:
    snapshot = builtins.get_snapshot()
    def start_of_day(value: datetime.datetime) -> datetime.datetime:
        _snapshot_bound('$start_of_day', snapshot)
        _assert_datetime_argument(value, 1)
        return snapshot.start_of_day(value)  # type: ignore[union-attr]
    return start_of_day

def _builtin_add_days(builtins: 'Builtins') -> Callable[..., Any]:
    snapshot = builtins.get_snapshot()
    def add_days(value: datetime.datetime, days: Any) -> datetime.datetime:
        _snapshot_bound('$add_days', snapshot)
        _assert_datetime_argument(value, 1)
        return snapshot.add_calendar_days(_assert_integer_argument(days, 2, 'days'), moment=value)  # type: ignore[union-attr]
    return add_days

def _builtin_add_months(builtins: 'Builtins') -> Callable[..., Any]:
    snapshot = builtins.get_snapshot()
    def add_months(value: datetime.datetime, months: Any) -> datetime.datetime:
        _snapshot_bound('$add_months', snapshot)
        _assert_datetime_argument(value, 1)
        return snapshot.add_calendar_months(_assert_integer_argument(months, 2, 'months'), moment=value)  # type: ignore[union-attr]
    return add_months

def _builtin_add_business_days(builtins: 'Builtins') -> Callable[..., Any]:
    snapshot = builtins.get_snapshot()
    def add_business_days(value: datetime.datetime, days: Any) -> datetime.datetime:
        _snapshot_bound('$add_business_days', snapshot)
        _assert_datetime_argument(value, 1)
        count = _assert_integer_argument(days, 2, 'days')
        try:
            return snapshot.add_business_days(count, moment=value)  # type: ignore[union-attr]
        except errors.EvaluationError:
            raise errors.FunctionCallError('$add_business_days requires a business calendar on the temporal snapshot') from None
    return add_business_days

def _builtin_is_holiday(builtins: 'Builtins') -> Callable[..., Any]:
    snapshot = builtins.get_snapshot()
    def is_holiday(value: datetime.datetime | None = None) -> bool:
        _snapshot_bound('$is_holiday', snapshot)
        if value is not None:
            _assert_datetime_argument(value, 1)
        try:
            return snapshot.is_holiday(value)  # type: ignore[union-attr]
        except errors.EvaluationError:
            raise errors.FunctionCallError('$is_holiday requires a business calendar on the temporal snapshot') from None
    return is_holiday

def _builtin_is_business_day(builtins: 'Builtins') -> Callable[..., Any]:
    snapshot = builtins.get_snapshot()
    def is_business_day(value: datetime.datetime | None = None) -> bool:
        _snapshot_bound('$is_business_day', snapshot)
        if value is not None:
            _assert_datetime_argument(value, 1)
        try:
            return snapshot.is_business_day(value)  # type: ignore[union-attr]
        except errors.EvaluationError:
            raise errors.FunctionCallError('$is_business_day requires a business calendar on the temporal snapshot') from None
    return is_business_day

def _builtin_range(start: Any, stop: Any = None, step: Any = None) -> list[int]:
    if not types.is_integer_number(start):
        raise errors.FunctionCallError('argument #1 (start) must be an integer number')
    if stop is not None:
        if not types.is_integer_number(stop):
            raise errors.FunctionCallError('argument #2 (stop) must be an integer number')
        if step is not None:
            if not types.is_integer_number(step):
                raise errors.FunctionCallError('argument #3 (step) must be an integer number')
            return list(range(int(start), int(stop), int(step)))
        return list(range(int(start), int(stop)))
    return list(range(int(start)))

def _builtins_split(string: str, sep: str | None = None, maxsplit: Any = None) -> tuple[str, ...]:
    if maxsplit is None:
        maxsplit = -1
    elif types.is_natural_number(maxsplit):
        maxsplit = int(maxsplit)
    else:
        raise errors.FunctionCallError('argument #3 (maxsplit) must be a natural number')
    return tuple(string.split(sep=sep, maxsplit=maxsplit))

class BuiltinValueGenerator(object):
    """项目内部接口说明。"""
    __slots__ = ('callable',)
    callable: Callable[['Builtins'], Any]
    def __init__(self, callable: Callable[['Builtins'], Any]) -> None:
        self.callable = callable

    def __call__(self, builtins: 'Builtins') -> Any:
        return self.callable(builtins)

class Builtins(collections.abc.Mapping):
    """项目内部接口说明。"""
    scope_name = 'built-in'
    """The identity name of the scope for builtin symbols."""
    def __init__(
            self,
            values: Mapping[str, Any],
            namespace: str | None = None,
            timezone: datetime.tzinfo | None = None,
            value_types: Mapping[str, 'types._DataTypeDef'] | None = None,
            snapshot_provider: Callable[[], TemporalSnapshot | None] | None = None
    ) -> None:
        """项目内部接口说明。"""
        self.__values = values
        self.__value_types = value_types or {}
        self.namespace = namespace
        self.timezone = timezone or dateutil.tz.tzlocal()
        self.__snapshot_provider = snapshot_provider or (lambda: None)

    def get_snapshot(self) -> TemporalSnapshot | None:
        """返回当前评估线程上生效的时态快照；未建立快照时为 ``None``。"""
        return self.__snapshot_provider()

    def resolve_type(self, name: str) -> 'types._DataTypeDef':
        """项目内部接口说明。"""
        return self.__value_types.get(name, types.DataType.UNDEFINED)

    def __repr__(self) -> str:
        return "<{} namespace={!r} keys={!r} timezone={!r} >".format(self.__class__.__name__, self.namespace, tuple(self.keys()), self.timezone)

    def __getitem__(self, name: str) -> Any:
        value = self.__values[name]
        if isinstance(value, collections.abc.Mapping):
            if self.namespace is None:
                namespace = name
            else:
                namespace = self.namespace + '.' + name
            return self.__class__(
                    value,
                    namespace=namespace,
                    timezone=self.timezone,
                    snapshot_provider=self.__snapshot_provider
            )
        elif callable(value) and isinstance(value, BuiltinValueGenerator):
            value = value(self)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    @classmethod
    def from_defaults(cls, values: Mapping[str, Any] | None = None, **kwargs: Any) -> 'Builtins':
        """项目内部接口说明。"""
        now = BuiltinValueGenerator(_builtin_now)
        # there may be errors here if the decimal.Context precision exceeds what is provided by the math constants
        default_values = {
                # mathematical constants
                'e': decimal.Decimal(repr(math.e)),
                'pi': decimal.Decimal(repr(math.pi)),
                # timestamps
                'now': now,
                'today': BuiltinValueGenerator(_builtin_today),
                # temporal functions derived from the per-evaluation temporal snapshot
                'start_of_day': BuiltinValueGenerator(_builtin_start_of_day),
                'add_days': BuiltinValueGenerator(_builtin_add_days),
                'add_months': BuiltinValueGenerator(_builtin_add_months),
                'add_business_days': BuiltinValueGenerator(_builtin_add_business_days),
                'is_holiday': BuiltinValueGenerator(_builtin_is_holiday),
                'is_business_day': BuiltinValueGenerator(_builtin_is_business_day),
                # functions
                'abs': abs,
                'any': any,
                'all': all,
                'sum': sum,
                'map': _builtin_map,
                'max': max,
                'min': min,
                'filter': _builtin_filter,
                'parse_datetime': BuiltinValueGenerator(_builtin_parse_datetime_generator),
                'parse_float': parse_float,
                'parse_timedelta': parse_timedelta,
                'random': _builtin_random,
                'range': _builtin_range,
                'split': _builtins_split
        }
        default_values.update(values or {})
        default_value_types = {
                # mathematical constants
                'e': types.DataType.FLOAT,
                'pi': types.DataType.FLOAT,
                # timestamps
                'now': types.DataType.DATETIME,
                'today': types.DataType.DATETIME,
                # temporal functions
                'start_of_day': types.DataType.FUNCTION(
                        'start_of_day', return_type=types.DataType.DATETIME, argument_types=(types.DataType.DATETIME,)),
                'add_days': types.DataType.FUNCTION(
                        'add_days', return_type=types.DataType.DATETIME,
                        argument_types=(types.DataType.DATETIME, types.DataType.FLOAT)),
                'add_months': types.DataType.FUNCTION(
                        'add_months', return_type=types.DataType.DATETIME,
                        argument_types=(types.DataType.DATETIME, types.DataType.FLOAT)),
                'add_business_days': types.DataType.FUNCTION(
                        'add_business_days', return_type=types.DataType.DATETIME,
                        argument_types=(types.DataType.DATETIME, types.DataType.FLOAT)),
                'is_holiday': types.DataType.FUNCTION(
                        'is_holiday', return_type=types.DataType.BOOLEAN,
                        argument_types=(types.DataType.DATETIME,), minimum_arguments=0),
                'is_business_day': types.DataType.FUNCTION(
                        'is_business_day', return_type=types.DataType.BOOLEAN,
                        argument_types=(types.DataType.DATETIME,), minimum_arguments=0),
                # functions
                'abs': types.DataType.FUNCTION('abs', return_type=types.DataType.FLOAT, argument_types=(types.DataType.FLOAT,)),
                'all': types.DataType.FUNCTION('all', return_type=types.DataType.BOOLEAN, argument_types=(types.DataType.ARRAY,)),
                'any': types.DataType.FUNCTION('any', return_type=types.DataType.BOOLEAN, argument_types=(types.DataType.ARRAY,)),
                'sum': types.DataType.FUNCTION('sum', return_type=types.DataType.FLOAT, argument_types=(types.DataType.ARRAY(types.DataType.FLOAT),)),
                'map': types.DataType.FUNCTION('map', return_type=types.DataType.ARRAY, argument_types=(types.DataType.FUNCTION, types.DataType.ARRAY)),
                'max': types.DataType.FUNCTION('max', return_type=types.DataType.FLOAT, argument_types=(types.DataType.ARRAY(types.DataType.FLOAT),)),
                'min': types.DataType.FUNCTION('min', return_type=types.DataType.FLOAT, argument_types=(types.DataType.ARRAY(types.DataType.FLOAT),)),
                'filter': types.DataType.FUNCTION('filter', return_type=types.DataType.ARRAY, argument_types=(types.DataType.FUNCTION, types.DataType.ARRAY)),
                'parse_datetime': types.DataType.FUNCTION('parse_datetime', return_type=types.DataType.DATETIME, argument_types=(types.DataType.STRING,)),
                'parse_float': types.DataType.FUNCTION('parse_float', return_type=types.DataType.FLOAT, argument_types=(types.DataType.STRING,)),
                'parse_timedelta': types.DataType.FUNCTION('parse_timedelta', return_type=types.DataType.TIMEDELTA, argument_types=(types.DataType.STRING,)),
                'random': types.DataType.FUNCTION('random', return_type=types.DataType.FLOAT, argument_types=(types.DataType.FLOAT,), minimum_arguments=0),
                'range': types.DataType.FUNCTION('range', return_type=types.DataType.ARRAY(types.DataType.FLOAT), argument_types=(types.DataType.FLOAT, types.DataType.FLOAT, types.DataType.FLOAT,), minimum_arguments=1),
                'split': types.DataType.FUNCTION(
                        'split',
                        return_type=types.DataType.ARRAY(types.DataType.STRING),
                        argument_types=(types.DataType.STRING, types.DataType.STRING, types.DataType.FLOAT),
                        minimum_arguments=1
                )
        }
        default_value_types.update(kwargs.pop('value_types', {}))
        return cls(default_values, value_types=default_value_types, **kwargs)
