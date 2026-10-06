# 规则表达式引擎

本项目提供可嵌入服务端的规则解析、类型检查、属性解析和表达式评估能力。生产源码位于 `lib/rule_engine/`，核心回归测试位于 `tests/`。

## 安装

`python3 -m pip install --break-system-packages --no-build-isolation -e .`

## 测试

`python3 -m pytest -q`

## 构建

`python3 -m compileall -q lib/rule_engine`

`python3 -m build --wheel --no-isolation`

## 使用

调用方创建规则并传入普通 Python 对象即可完成本地评估，不需要外部服务。

```python
import rule_engine
rule = rule_engine.Rule('name == "Alice" and age >= 18')
rule.matches({'name': 'Alice', 'age': 30})  # True
```

## 时态快照（可重放的时间语义）

引用 `$now` / `$today` / 节假日 / 宽限期的规则，如果在评估中直接读取系统时钟，同一份输入在几分钟后
就可能得到不同结果。引擎为**每次评估**提供不可变的时态快照，统一冻结：

* **业务时刻** `moment` —— `$now`、`$today` 及一切窗口/截断/持续时间推导的唯一来源；
* **时区数据库版本** `timezone_version` —— 自动探测 IANA tzdata 版本并冻结到快照；
* **日历版本** `calendar_version` —— 节假日表（`BusinessCalendar`）的不可变版本。

### 重放历史决定

```python
import datetime
from rule_engine import Context, Rule, TemporalSnapshot

ctx = Context(default_timezone='America/New_York')
rule = Rule('$now >= policy_start and expiry >= $now', context=ctx)

# 批量重放：每条决定携带其业务时刻，结果不受当前时钟影响
decision_moment = datetime.datetime(2022, 3, 13, 12, 30, tzinfo=...)
rule.matches(thing, moment=decision_moment)
# 或直接传入快照对象
snap = TemporalSnapshot.frozen('2022-03-13T12:30:00-04:00', 'America/New_York')
rule.matches(thing, snapshot=snap)
print(snap.versions)          # {'timezone': '2026.5', 'calendar': None} —— 审计/持久化本次决定的版本

# 多条规则共享同一业务时刻
with ctx.temporal_snapshot(moment=decision_moment) as snap:
    rule1.matches(thing)
    rule2.matches(thing)
```

### 节假日与宽限期

```python
from rule_engine import BusinessCalendar

calendar = BusinessCalendar([datetime.date(2022, 3, 14)], version='holidays-2022.1')
ctx = Context(default_timezone='America/New_York', calendar=calendar)

Rule('$is_holiday(incident_at)', context=ctx).matches(thing, moment=decision_moment)
Rule('$add_business_days(deadline, 60) >= $now', context=ctx).matches(thing, moment=decision_moment)
# 规则外也可直接使用快照的宽限期判定（含等号、固定时长或工作日）
snap.within_grace_period(deadline, datetime.timedelta(days=60), business_days=True)
```

规则内新增的时态函数：`$start_of_day`、`$add_days`、`$add_months`、`$add_business_days`、
`$is_holiday`、`$is_business_day`。

### 夏令时语义

* **重叠时刻**（秋季回拨）：默认取较早一次，可用 `disambiguation='later'` 或 `'raise'`；
* **缺失时刻**（春季前拨）：默认 `gap_policy='raise'` 抛出 `TemporalAmbiguityError`，
  也可 `'forward'` / `'backward'`；
* `datetime ± t'P…'`（规则算术）保持**本地墙上钟点**（日历日语义，"明天同一钟点"），结果落在
  夏令时 gap/重叠时按快照策略解析；`snapshot.shift(delta)` 则在 UTC 瞬时上加**固定物理时长**
  （24 小时恒为 24 小时，钟点可能改变）。日期截断到午夜时，若该时区午夜发生跳变，端点取
  "当日第一个有效瞬间"。

```python
snap = TemporalSnapshot.frozen('2022-03-13T12:00:00-04:00', 'America/New_York', gap_policy='raise')
# 重放时可校验版本，环境 tzdata 与记录不一致即抛 TemporalVersionMismatchError
snap = TemporalSnapshot.frozen(..., timezone_version='2024a', verify_versions=True)
```

### 嵌套评估与自定义时间函数

自定义函数中再次调用 `rule.evaluate(...)` 会继承外层快照；传入自己的 `snapshot=` 可覆盖，退出后自动
恢复外层。自定义内置函数通过 `builtins.get_snapshot()` 读取当前快照。

### 成本

不引用任何时间符号的规则不会建立快照、不读取时钟、不探测 tzdata 版本，热路径只增加少量标志位读写。
