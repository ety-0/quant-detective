# A 股涨停接力 · 第一步：每日股池快照与情绪档位

只读研究模块。每个交易日抓一次涨停池、炸板池、跌停池和昨日涨停池，给当日一个描述性的情绪档位。不下单，不产生交易指令，也不产生候选股。候选的锁定和次日打分属于下一步，届时接 `time_machine`（PREDICT → LOCK → REVEAL → SCORE）。

## 用法

```bash
uv sync --frozen
uv run qd-ashare snapshot --date 2026-11-16     # 四次公开 GET，随后输出档位
uv run qd-ashare sentiment --date 2026-11-16    # 不联网，用已归档的原始响应重算
```

加 `--json` 输出完整情绪记录。日期也接受 `20261116`。退出码 0 表示档位可用，2 表示请求被拒绝、快照不完整或档位不可用。

每日流程：15:05（Asia/Shanghai）之后运行 `snapshot`，确认输出写着“正式”，再把当日清单提交进 Git。

## 文件去向

| 内容 | 位置 | 进 Git |
|---|---|---|
| 原始响应（逐字节） | `market_cache/ashare_limitup/raw/<日期>/<股池>.json` | 否，`market_cache/` 已忽略 |
| 情绪记录（聚合指标） | `market_cache/ashare_limitup/derived/<日期>/sentiment.json` | 否 |
| 正式清单：请求回执、行数、SHA-256 | `research_outputs/ashare_limitup/manifests/<日期>.json` | 是 |
| 临时清单（盘中或不完整） | `market_cache/ashare_limitup/provisional/<日期>.json` | 否 |

清单不含任何个股行。只有正式快照才会写进 `research_outputs/`，盘中看一眼不会在那里留下文件。`make verify` 会检查仓库里每份清单都是正式快照、只含约定字段。原始响应丢失后无法按原哈希重建，需要自行备份 `market_cache/ashare_limitup/`。

## 指标定义

口径是东方财富“涨停板行情”页面：不含 ST 与科创板，涨停池和昨日涨停池不含未中断连续一字板的新股。所以这里的涨停数不是全市场涨停数。

| 指标 | 定义 |
|---|---|
| `limit_up_count` 涨停数 | 涨停池行数 |
| `limit_down_count` 跌停数 | 跌停池行数 |
| `broken_board_rate` 炸板率 | 炸板池行数 ÷（涨停池行数 + 炸板池行数） |
| `max_consecutive_boards` 最高连板 | 涨停池里最大的连板数；涨停池为空时是 0 |
| `promotion_rate` 晋级率 | 昨日涨停池里今天仍在涨停池的比例，按代码匹配 |
| `yesterday_limit_up_avg_change_pct` 昨日涨停今日表现 | 昨日涨停池今日涨跌幅的算术平均；记录里另有中位数和红盘率 |

某个股池不可用，或者分母为 0 时，对应指标是 `null`，不会写成 0。

## 情绪档位

六个指标各按 `config/ashare_limitup.json` 里的一对阈值打 −1 / 0 / +1（阈值含端点），求和得到 −6 到 +6 的分数，再按 `tiers` 映射：冰点、偏冷、中性、偏热、高潮。任何一个指标缺失，档位就是“不可用”，不会按中性处理。

档位是对当日已发生状态的描述（REPORTED），不是预测。

**配置里的阈值是初始经验值，没有做过回测或样本外校准**，`calibration_status` 写的是 `UNCALIBRATED_HEURISTIC`。每条情绪记录带 `config_sha256`，改阈值后可以分辨记录出自哪套阈值。

## 快照何时算“正式”

`final: true` 需要同时满足：四个股池都拿到可用响应、不全为空、且每个股池都是在当日 `final_after_local_time`（默认 15:05）之后收到的。正式清单不可覆盖，再次抓取会报 `SNAPSHOT_LOCKED`；临时清单会被之后的抓取替换，正式快照落盘后自动删除。

以下情况在发请求之前就拒绝：

| 代码 | 原因 |
|---|---|
| `NON_SESSION` | 不是上交所交易日 |
| `FUTURE_DATE` | 晚于上海当前日期 |
| `CAPTURE_LAG_EXCEEDED` | 距今超过 `max_capture_lag_days`（默认 14 天） |
| `INVALID_DATE` | 日期格式不对 |

单个股池的状态：`OK`、`FETCH_FAILED`、`NON_JSON`、`PROVIDER_ERROR`、`SCHEMA_MISMATCH`、`TRUNCATED`、`DATE_AHEAD_OF_PROVIDER`。四个股池都为空时整份快照记为 `NO_PROVIDER_DATA`。

## 数据源行为（2026-11-17 实测，随时可能变）

这些观察决定了上面的拒绝规则。端点是 `push2ex.eastmoney.com` 的 `getTopicZTPool`、`getTopicZBPool`、`getTopicDTPool`、`getYesterdayZTPool`。

- 历史日期按 `date` 参数返回当日股池。响应里的 `qdate` 是服务端当前日期，不是数据日期，不能用来核对数据属于哪一天。
- 未来日期返回的是今天的实时股池。
- 周日返回 `tc: 0` 的空股池。距今 29 天的交易日四个股池同样全空，距今 20 天的还有数据。空股池本身分不清“当天没有”和“已经查不到”，所以快照只允许短滞后，并且四池全空不记为零。
- `ut` 和 `dpt` 两个参数缺一个就返回 `rc: 102`。`ut` 是该页面固定带的值，取自 AKShare 源码，不是账户凭证。
- 盘中请求当天日期，返回的是还在变化的实时股池。

## 尚未验证

- 15:05 时数据源的股池是否已经定稿，只做了“次日上午抓前一日”的实测。
- 同一历史日期隔天再抓，内容是否完全一致。
- 数据源的限流阈值、境内外 IP 差异。实测只有从一个境外出口发出的几十次请求。
- `exchange-calendars==4.13.2` 的上交所日历只到 2026-12-31。之后的工作日仍可抓取，但清单会标 `UNVERIFIED_OUT_OF_CALENDAR_BOUNDS`，节假日要靠“四池全空”兜底。2027 年前需要升级该依赖。
- 北交所股票是否在口径内。实测当天股池里只见到 00、30、60 开头的代码。

## 上游参考

字段命名和连板梯队排序参考 ZhuLinsen/daily_stock_analysis 的 `get_limit_up_pool`，端点和列含义参考 AKShare 的 `stock_ztb_em.py`，两者都是 MIT 许可，都没有被安装或导入，见 `THIRD_PARTY_NOTICES.md`。和参考实现不同的地方：保留整个股池而不是前 N 条；缺失值保持缺失，不转成 0；不轮换 User-Agent、不重试；响应不可用时给出状态，不吞掉异常。
