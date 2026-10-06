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

## 时态快照（确定性评估与历史重放）

续保类规则同时引用保单时区、节假日与宽限期，若在规则中直接读取墙上时钟，同一条规则在几分钟后批量重放就可能得到不同结果。时态快照把一次评估所需的全部时态输入固定下来：

```python
import datetime
from rule_engine import Calendar, Context, Rule, TemporalSnapshot

context = Context(default_timezone='America/New_York')
snapshot = TemporalSnapshot(
    datetime.datetime(2024, 11, 3, 12, 0, tzinfo=datetime.timezone.utc),  # 业务时刻
    timezone='America/New_York',                                          # 保单时区
    calendar=Calendar([datetime.date(2024, 11, 5)], version='hol-2024.11'),
    dst_policy='raise',                                                   # gap/重叠时刻必须显式处理
)

rule = Rule('$within(policy_expires_at, $window(t"P30D")[0], $window(t"P30D")[1])', context=context)
rule.matches(thing, at=snapshot)          # 同一快照任意次重放结果一致
rule.filter(batch_of_things, at=snapshot)  # 批量评估共享同一快照
```

快照提供并固定三类版本，可随业务决定落库：

- 业务时刻：`$now`/`$today` 的来源（`$now` 以保单时区表达，`$today` 在保单时区下截断到零点）；
- 时区数据库版本：快照记录实际使用的 tzdata/IANA 版本（如 `system/iana-2026b`、`tzdata/2026.5`）；
- 日历版本：节假日/工作日表及其内容指纹。

规则中的时态运算全部从快照推导：

| 内置函数 | 语义 |
| --- | --- |
| `$window(timedelta)` | 以业务时刻为右端点的半开区间 `[now-duration, now)` |
| `$window_days(n)` | 按保单时区日界对齐的 n 天半开区间（正确处理跨日） |
| `$within(t, start, end)` | 时刻是否落在半开区间（按绝对时刻比较） |
| `$start_of(t, unit)` | 日期截断，`unit` 为 `day`/`week`/`month`/`year`（周以周一开始） |
| `$is_holiday([t])` / `$is_business_day([t])` | 按快照日历判断，缺省取业务时刻 |
| `$add_business_days(t, n)` | 按日历跳过周末与节假日，支持负数 |
| `$parse_datetime(s)` | 无时区字符串按快照保单时区与 DST 策略解释 |

夏令时语义由 `dst_policy` 明确规定：

- `raise`（默认）：春季缺失时刻（gap）与秋季重叠时刻（fold）都抛出 `TemporalError`；
- `raise_on_gap`：gap 抛错，fold 取日间侧（`fold=0`）；
- `shift_forward`：gap 向后推移到过渡结束点，fold 取日间侧；
- `shift_backward`：gap 前移到过渡开始点，fold 取标准侧（`fold=1`）。

历史重放与版本确认：

```python
record = snapshot.to_json()                 # 与决定一起持久化
restored = TemporalSnapshot.from_json(record)  # 重建时校验指纹，篡改即报错
restored.compatible_with(snapshot)          # 时区/tzdata/日历/DST 策略一致才能重放
print(restored.versions)                    # 写入审计日志的版本字典
```

嵌套评估（自定义函数内再评估另一条规则）会自动隔离内层快照，退出后恢复外层快照；内层不传 `at=` 时继承外层快照。自定义时间函数用 `@temporal_function` 声明，调用时自动注入当前快照；离开快照作用域调用会显式报错，不会悄悄退回墙上时钟。

未传 `at=` 的传统评估路径完全不变：不创建快照、不做 DST 检查，不使用时间的规则不承担额外成本。

