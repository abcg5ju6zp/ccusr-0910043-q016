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
from .parser.utilities import parse_float, parse_timedelta

import dateutil.parser
import dateutil.tz

def _temporal():
    # delayed import to avoid a circular dependency during package initialization
    from .engine import temporal
    return temporal

def _current_snapshot():
    """返回当前评估作用域内的时态快照；未使用快照时为 None（零成本路径）。"""
    return _temporal().get_current_snapshot()

def _builtin_filter(function: Callable[[Any], Any], iterable: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(filter(function, iterable))

def _builtin_map(function: Callable[[Any], Any], iterable: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(map(function, iterable))

def _builtin_parse_datetime(builtins: 'Builtins', string: str) -> datetime.datetime:
    snapshot = _current_snapshot()
    try:
        value = dateutil.parser.isoparse(string)
    except ValueError:
        raise errors.DatetimeSyntaxError('invalid datetime literal', string) from None
    if value.tzinfo is None:
        if snapshot is not None:
            # naive wall times are interpreted in the snapshot's policy timezone, applying its DST semantics
            value = snapshot.localize(value)
        else:
            value = value.replace(tzinfo=builtins.timezone)
    return value

def _builtin_random(boundary: Any = None) -> Any:
    if boundary is not None:
        if not types.is_natural_number(boundary):
            raise errors.FunctionCallError('argument #1 (boundary) must be a natural number')
        return random.randint(0, int(boundary))
    return random.random()

def _builtin_now(builtins: 'Builtins') -> datetime.datetime:
    snapshot = _current_snapshot()
    if snapshot is not None:
        return snapshot.now
    return datetime.datetime.now(tz=builtins.timezone)

def _builtin_today(builtins: 'Builtins') -> datetime.datetime:
    snapshot = _current_snapshot()
    if snapshot is not None:
        # truncate against the snapshot's business timezone so day boundaries are replay-stable
        return snapshot.today()
    return _builtin_now(builtins).replace(hour=0, minute=0, second=0, microsecond=0)

# ---------------------------------------------------------------------------
# Temporal builtins (windows, truncation and calendar-aware arithmetic)
#
# Every temporal builtin derives its result from the active TemporalSnapshot
# rather than the wall clock, so identical (rule, thing, snapshot) triples
# always evaluate identically and can be replayed historically.
# ---------------------------------------------------------------------------
def _require_snapshot():
    snapshot = _current_snapshot()
    if snapshot is None:
        raise errors.FunctionCallError(
                'this temporal builtin requires a TemporalSnapshot; pass one to Rule.evaluate '
                '(e.g. rule.evaluate(thing, at=TemporalSnapshot.current(...)))')
    return snapshot

def _as_datetime(value: Any, position: int) -> datetime.datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        raise errors.FunctionCallError('argument #{} must be a datetime value'.format(position))
    return value

def _builtin_start_of(value: Any, unit: str = 'day') -> datetime.datetime:
    snapshot = _require_snapshot()
    return snapshot.start_of(unit, _as_datetime(value, 1))

def _builtin_window(duration: Any, anchor: Any = None) -> tuple[datetime.datetime, datetime.datetime]:
    snapshot = _require_snapshot()
    if not isinstance(duration, datetime.timedelta):
        raise errors.FunctionCallError('argument #1 (duration) must be a timedelta value')
    return snapshot.window(duration, anchor=_as_datetime(anchor, 2))

def _builtin_window_days(days: Any) -> tuple[datetime.datetime, datetime.datetime]:
    snapshot = _require_snapshot()
    if not types.is_natural_number(days):
        raise errors.FunctionCallError('argument #1 (days) must be a natural number')
    return snapshot.window_days(int(days))

def _builtin_within(value: Any, start: Any, end: Any) -> bool:
    snapshot = _require_snapshot()
    return snapshot.within(_as_datetime(value, 1), _as_datetime(start, 2), _as_datetime(end, 3))

def _builtin_is_holiday(value: Any = None) -> bool:
    snapshot = _require_snapshot()
    return snapshot.is_holiday(None if value is None else _as_datetime(value, 1))

def _builtin_is_business_day(value: Any = None) -> bool:
    snapshot = _require_snapshot()
    return snapshot.is_business_day(None if value is None else _as_datetime(value, 1))

def _builtin_add_business_days(value: Any, count: Any) -> datetime.datetime:
    snapshot = _require_snapshot()
    if not types.is_integer_number(count):
        raise errors.FunctionCallError('argument #2 (count) must be an integer number')
    return snapshot.add_business_days(_as_datetime(value, 1), int(count))

def _builtin_parse_datetime_generator(builtins: 'Builtins') -> 'functools.partial[datetime.datetime]':
    return functools.partial(_builtin_parse_datetime, builtins)

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
            value_types: Mapping[str, 'types._DataTypeDef'] | None = None
    ) -> None:
        """项目内部接口说明。"""
        self.__values = values
        self.__value_types = value_types or {}
        self.namespace = namespace
        self.timezone = timezone or dateutil.tz.tzlocal()

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
            return self.__class__(value, namespace=namespace, timezone=self.timezone)
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
                'split': _builtins_split,
                # temporal operations derived from the active TemporalSnapshot
                'start_of': _builtin_start_of,
                'window': _builtin_window,
                'window_days': _builtin_window_days,
                'within': _builtin_within,
                'is_holiday': _builtin_is_holiday,
                'is_business_day': _builtin_is_business_day,
                'add_business_days': _builtin_add_business_days
        }
        default_values.update(values or {})
        default_value_types = {
                # mathematical constants
                'e': types.DataType.FLOAT,
                'pi': types.DataType.FLOAT,
                # timestamps
                'now': types.DataType.DATETIME,
                'today': types.DataType.DATETIME,
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
                ),
                # temporal operations: all results are derived from the TemporalSnapshot
                'start_of': types.DataType.FUNCTION(
                        'start_of',
                        return_type=types.DataType.DATETIME,
                        argument_types=(types.DataType.DATETIME, types.DataType.STRING),
                        minimum_arguments=1
                ),
                'window': types.DataType.FUNCTION(
                        'window',
                        return_type=types.DataType.ARRAY(types.DataType.DATETIME),
                        argument_types=(types.DataType.TIMEDELTA, types.DataType.DATETIME),
                        minimum_arguments=1
                ),
                'window_days': types.DataType.FUNCTION(
                        'window_days',
                        return_type=types.DataType.ARRAY(types.DataType.DATETIME),
                        argument_types=(types.DataType.FLOAT,)
                ),
                'within': types.DataType.FUNCTION(
                        'within',
                        return_type=types.DataType.BOOLEAN,
                        argument_types=(types.DataType.DATETIME, types.DataType.DATETIME, types.DataType.DATETIME)
                ),
                'is_holiday': types.DataType.FUNCTION(
                        'is_holiday',
                        return_type=types.DataType.BOOLEAN,
                        argument_types=(types.DataType.DATETIME,),
                        minimum_arguments=0
                ),
                'is_business_day': types.DataType.FUNCTION(
                        'is_business_day',
                        return_type=types.DataType.BOOLEAN,
                        argument_types=(types.DataType.DATETIME,),
                        minimum_arguments=0
                ),
                'add_business_days': types.DataType.FUNCTION(
                        'add_business_days',
                        return_type=types.DataType.DATETIME,
                        argument_types=(types.DataType.DATETIME, types.DataType.FLOAT),
                        minimum_arguments=1
                )
        }
        default_value_types.update(kwargs.pop('value_types', {}))
        return cls(default_values, value_types=default_value_types, **kwargs)
