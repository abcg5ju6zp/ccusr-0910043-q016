#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/engine/rule.py
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

import decimal
from typing import Any, Iterable, Iterator, TYPE_CHECKING

from .. import errors
from ..parser import Parser
from ..temporal import BusinessCalendar, TemporalSnapshot
from .context import Context

if TYPE_CHECKING:
    import graphviz
    import datetime

class Rule(object):
    """项目内部接口说明。"""
    parser: Parser = Parser()
    """
    The :py:class:`~rule_engine.parser.Parser` instance that will be used for parsing the rule text into a compatible
    用于规则求值的抽象语法树（AST）。
    """
    def __init__(self, text: str, context: Context | None = None) -> None:
        """项目内部接口说明。"""
        context = context or Context()
        self.text = text
        self.context = context
        self.statement = self.parser.parse(text, context)

    def __getstate__(self) -> dict[str, Any]:
        return {'text': self.text, 'context': self.context}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.text = state['text']
        self.context = state['context']
        self.statement = self.parser.parse(self.text, self.context)

    def __repr__(self) -> str:
        return "<{0} text={1!r} >".format(self.__class__.__name__, self.text)

    def __str__(self) -> str:
        return self.text

    def filter(
            self,
            things: Iterable[Any],
            *,
            snapshot: TemporalSnapshot | None = None,
            moment: 'datetime.datetime | None' = None,
            calendar: BusinessCalendar | None = None
    ) -> Iterator[Any]:
        """项目内部接口说明。"""
        yield from (
                thing for thing in things
                if self.matches(thing, snapshot=snapshot, moment=moment, calendar=calendar)
        )

    @classmethod
    def is_valid(cls, text: str, context: Context | None = None) -> bool:
        """项目内部接口说明。"""
        try:
            cls.parser.parse(text, (context or Context()))
        except errors.EngineError:
            return False
        return True

    def evaluate(
            self,
            thing: Any,
            *,
            snapshot: TemporalSnapshot | None = None,
            moment: 'datetime.datetime | None' = None,
            calendar: BusinessCalendar | None = None
    ) -> Any:
        """项目内部接口说明。

        时间确定性：

        * 不传 *snapshot* / *moment* 时，规则中的 ``$now`` / ``$today`` 在*首次被引用时*惰性读取一次
          系统时钟，同一次评估内保持恒定；不引用任何时间符号的规则不会读取时钟，也不承担快照成本；
        * 传入 *moment*（或 *snapshot*）时，整次评估在该业务时刻上重放，结果可复现；
        * *calendar* 可随 *moment* 一起覆盖 Context 的默认节假日日历；
        * 自定义函数内部嵌套调用 :meth:`evaluate` 时继承外层快照，除非显式传入自己的快照。
        """
        if snapshot is None and moment is not None:
            snapshot = TemporalSnapshot.create(
                    moment,
                    timezone=self.context.default_timezone,
                    calendar=self.context.calendar if calendar is None else calendar,
                    disambiguation=self.context.disambiguation,
                    gap_policy=self.context.gap_policy,
            )
        elif calendar is not None:
            raise ValueError('calendar can only be provided together with moment or snapshot')
        # 评估生命周期（内联以保持非时间规则的热路径与旧实现同构：一次 TLS 访问、无元组/额外调用）。
        tls = self.context._tls
        top_level = not tls.evaluation_active
        if top_level:
            # 内联的单次评估清理：上下文管理器拥有的 temporal_frames 必须保留；空容器不重复清理。
            if tls.assignment_scopes:
                tls.assignment_scopes.clear()
            tls.regex_groups = None
            # auto_snapshot 每次顶层评估都必须失效，否则会复用上次评估的旧业务时刻。
            tls.auto_snapshot = None
            tls.last_snapshot = None
        tls.evaluation_active = True
        pushed_frame = False
        if snapshot is not None:
            tls.temporal_frames.append(snapshot)
            tls.last_snapshot = snapshot
            pushed_frame = True
        elif tls.temporal_frames:
            # 继承外层（上下文管理器）显式快照。
            tls.last_snapshot = tls.temporal_frames[-1]
        # 无显式快照时 auto_snapshot 保持 None，仅在 $now 等时间符号首次被引用时惰性建立
        # （未使用时间的规则因此不读时钟、不探测 tzdata 版本，也不承担压栈成本）。
        try:
            with decimal.localcontext(self.context.decimal_context):
                return self.statement.evaluate(thing)
        finally:
            if pushed_frame:
                tls.temporal_frames.pop()
            if top_level:
                tls.evaluation_active = False

    def last_snapshot(self) -> TemporalSnapshot | None:
        """返回当前线程上最近一次评估实际使用的快照（未发生过评估或未引用时间符号时为 ``None``）。"""
        return self.context.last_temporal_snapshot()

    def matches(
            self,
            thing: Any,
            *,
            snapshot: TemporalSnapshot | None = None,
            moment: 'datetime.datetime | None' = None,
            calendar: BusinessCalendar | None = None
    ) -> bool:
        """项目内部接口说明。"""
        return bool(self.evaluate(thing, snapshot=snapshot, moment=moment, calendar=calendar))

    def to_graphviz(self) -> 'graphviz.Digraph':
        """项目内部接口说明。"""
        import graphviz
        digraph = graphviz.Digraph(comment=self.text)
        self.statement.to_graphviz(digraph)
        return digraph

class DebugRule(Rule):
    parser: Parser  # set per-instance in __init__ (overrides the class-level attribute on Rule)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.parser = Parser(debug=True)
        super(DebugRule, self).__init__(*args, **kwargs)
