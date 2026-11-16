# 数据定义 hydrocalendar

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 日值更正发布（M8，独立 `corr_*` 表）

### 10.1 更正文件列契约

| 规范列名 | 可接受别名 | 类型 | 可空 | 说明 |
|---|---|---|---|---|
| `station` | 站号 / 站点 / 水文站 / station_id / 测站 | 字符串 | 否 | 按字符串保形 |
| `date` | 日期 / 观测日期 / day | 日期 | 否 | 接受 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYYMMDD` |
| `rainfall_mm` | rainfall / 雨量 / 日雨量 / 降雨量 / 降水量 | 十进制或哨兵 | 否 | `0` 是零雨实测；哨兵（`缺测/-/N/A/空串…`）是缺测，二者分开 |
| `version` | 版本 / 数据版本 / ver / 版本号 | 整数 ≥ 1 | 是 | 缺省按 1；无法解析整行进隔离 |

三态：零雨（`kind=measured, value=0`，标 `K_CORR_ZERO_RAIN`）、缺测（`kind=missing, value=NULL`，标 `Q_CORR_MISSING`）、正常实测。缺测不按 0 计入月合计。

### 10.2 版本裁决（按站点 + 日期）

- 业务键 `(station, date)`；组内取 `version` 最大者，与导入先后无关（高版本裁决，非后写覆盖）。
- 最高版本下出现异值（含一实测一缺测）→ 整组进 `corr_day_conflicts`（`Q_CORR_VERSION_CONFLICT`），组内行全部落 `isolated`，该天不计合计、不计采信、计为隔离日。
- 最高版本多行同值 → 折叠为一条；其余低版本行留 `corr_daily_values` 作重复证据，不删除。
- 原始日值只追加（`corr_daily_values`），更正理由必填并随 `corr_runs.reason` 与世代 `corr_publishes.reason` 留痕。

### 10.3 月量与日历完整度

- 月合计 `Decimal` 求和后量化到 `0.1 mm`；缺测日不贡献数值，零雨贡献 0。
- 自然日按 `calendar.monthrange` 计算，闰月自动 29 天；`缺日 = 自然日 − 采信日 − 隔离日`，并在 `missing_dates` 显式列出每个缺日（闰月缺日标 `K_CORR_LEAP_MISSING` 语义）。
- 月状态：无缺日且无隔离日为 `complete`，否则 `partial`。

### 10.4 世代、并发与续跑

- 每次发布生成不可变世代 `corr_publishes`（`gen-*`），月量冻结在 `corr_monthly`；切换只改 `corr_state.current_gen` 指针，旧世代永不改写，旧报可原样复现。
- 并发：发布在 `BEGIN IMMEDIATE` 事务内复查 `corr_state.publish_lock`，第二个请求 409 `E_CORR_BUSY`。
- 过期：暂存记录 `base_gen`，发布时与当前世代比对，不一致即 409 `E_CORR_STALE`，旧版月报保持可读；可 `rebase` 改挂当前世代后再发布。
- 续跑：阶段进度写 `corr_runs.checkpoints_json`，半成品世代号写 `pending_gen`；重入先删半成品（绝不删当前世代）再重算，复用同一 `gen_id`，重试不重复生成月报。崩溃残留锁由服务首次构造时 `reset_stale_lock` 清理。
- 幂等：同一文件摘要（`source_digest`）的任务不重复建单、不重复插行；已 `done` 任务重复发布 409 `E_CORR_RUN_DONE`。

### 10.5 四账与导出

- 四账恒等：`输入 = 采纳 + 重复 + 隔离`（`GET /api/corrections/reconcile`，`delta=0` 才平衡）。
- 月报 `GET /api/corrections/monthly.csv?gen_id=...` 跟随所选世代；不带 `gen_id` 取当前世代。
- 前后版本对看 `GET /api/corrections/compare?left=&right=`，逐站逐月给出前后值与变化量。

M8 新增原因码：`E_CORR_EMPTY`、`E_CORR_HEADER`、`E_CORR_RUN_NOT_FOUND`、`E_CORR_RUN_NOT_STAGED`、`E_CORR_RUN_DONE`、`E_CORR_STALE`、`E_CORR_BUSY`、`E_CORR_VERSION`、`Q_CORR_BAD_DATE`、`Q_CORR_BAD_VALUE`、`Q_CORR_NEGATIVE`、`Q_CORR_MISSING`、`Q_CORR_VERSION_CONFLICT`、`K_CORR_ZERO_RAIN`、`K_CORR_HIGHER_VERSION`、`K_CORR_LOWER_DUPLICATE`、`K_CORR_LEAP_MISSING`、`K_CORR_RESUMED`。
