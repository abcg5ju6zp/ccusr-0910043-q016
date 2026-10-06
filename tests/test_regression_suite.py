"""运行项目 README 声明的核心回归测试。"""

import unittest
from importlib import import_module


engine = import_module("tests.engine")
parser = import_module("tests.parser")
thread_safety = import_module("tests.thread_safety")
temporal = import_module("tests.temporal")


EngineTests = engine.EngineTests
EngineRuleTests = engine.EngineRuleTests
EngineDatetimeRuleTests = engine.EngineDatetimeRuleTests
ContextTests = engine.ContextTests
ObjectTypeTests = engine.ObjectTypeTests
ContextSerializationTests = engine.ContextSerializationTests
ParserTests = parser.ParserTests
ParserLeftOperatorRightTests = parser.ParserLeftOperatorRightTests
ParserLiteralTests = parser.ParserLiteralTests
ThreadSafetyTests = thread_safety.ThreadSafetyTests

# 时态快照模块中的全部 TestCase（DST、窗口、重放、嵌套评估、版本确认等）
for _name, _obj in list(vars(temporal).items()):
    if isinstance(_obj, type) and issubclass(_obj, unittest.TestCase) and _obj is not unittest.TestCase:
        globals()[_name] = _obj
