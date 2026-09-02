# watchlist-scanner — 左右侧 watchlist 扫描器

每个美股交易日两次, 扫一遍 `watchlist.toml` 里的自选标的, 按
《危机黄金与左右侧交易-实战笔记》的规则机械化产出信号和具体合约建议:

- **开盘后 ~15 分钟** (布里斯班 23:45 / 00:45): 轻量异动报告 —
  隔夜 gap、进入价值区、财报临近、regime 变化、右侧持仓的盘中止损预警。
  **不出右侧确认** — 右侧信号以收盘为准。
- **尾盘前 ~15 分钟** (布里斯班 05:45 / 06:45): 完整信号引擎 —
  右侧确认(三选二: 不再新低/放量收复20日线/突破20日高, 且必须有真实回调
  作为前提)、每标的状态机、VIX/VIX3M 阶段门控、CSP 和 LEAP 合约票。

数据源: 全部 yfinance (~15min 延迟, 免费, 无 API key)。两个扫描窗口都在
美股盘中, 避开了 Yahoo 期权报价盘外归零的问题; 节假日/半日市由 SPY 1 分钟
K 线新鲜度门自动跳过。

## 安装

```bash
cd ~/Desktop/watchlist-scanner
python3 -m venv .venv
.venv/bin/pip install yfinance pandas numpy
.venv/bin/python test_signals.py            # 全绿再继续
cp com.zoez.watchlist-scanner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zoez.watchlist-scanner.plist
# 触发一次 macOS 通知权限弹窗, 务必点"允许" — 否则以后成功/失败/漏扫
# 通知全部会被系统静默丢弃
osascript -e 'display notification "通知已打通" with title "watchlist-scanner"'
```

### Windows (仅手动测试)

PowerShell (需要 Python 3.11+, `py --version` 确认):

```powershell
cd watchlist-scanner
py -3 -m venv .venv
.venv\Scripts\pip install yfinance pandas numpy tzdata
$env:PYTHONUTF8 = "1"
.venv\Scripts\python test_signals.py
.venv\Scripts\python scanner.py --mode close --force
```

两个 Windows 特有的坑已知/已处理: ① `zoneinfo` 在 Windows 没有系统时区库,
**必须装 `tzdata`**, 否则 import 就报 ZoneInfoNotFoundError; ② 中文
Windows 控制台默认 GBK, 打印中文/emoji 会 UnicodeEncodeError — 每个会话
先设 `$env:PYTHONUTF8="1"` (文件读写已在代码里固定 utf-8, 不受影响)。
launchd plist / run_scan.sh 是 macOS 专用, Windows 只手动跑; 要常驻另有
droplet 方案 (见下)。

launchd 每天在 8 个固定布里斯班时点触发 (23:45/00:45/01:30 开盘组,
05:45/06:00/06:45/07:00 尾盘组, 10:30 看门狗), `--mode auto` 只保留落在
美东窗口内的, 其余静默退出 —— 美国夏令时切换、美国周末 (布里斯班周六早=
美东周五尾盘)、单次网络故障 (每组有重试时点) 都不用管。10:30 的看门狗
检查当天美东交易日的两份报告是否都在, 缺了就发 "MISSED" 通知 (电脑睡眠
错过 launchd 时点, 或美股假日)。

## 手动运行

```bash
.venv/bin/python scanner.py --mode close --force          # 立刻跑完整尾盘扫描
.venv/bin/python scanner.py --mode open --force           # 开盘异动
.venv/bin/python scanner.py --mode close --force --tickers NVDA,MSFT
.venv/bin/python scanner.py --mode close --force --no-options   # 只看技术面(快)
```

加 `--email` 会在写完报告后通过 SMTP 推送 (环境变量配置, 见 droplet 节;
邮件失败 exit 1)。

`--force` 或 `--tickers` 视为手动测试运行: 报告写到 `*-manual.md`,
**不推进状态机、不写 IV 历史** — 盘中随便试跑, 不会污染当天真正的
定时扫描 (定时扫描的去重只看正式报告名)。盘外跑时合约价来自最近成交
(报告会标注), 仅供参考。

## 输出

尾盘报告结构 (为手机阅读优化): **今日动作**置顶 (3-6 行, 止损⚠️ →
出票🟢🔵🟣 → 被拦原因⏸ → 其余观望), 然后市场状态、概览表 (按可操作性
排序, 含"操作"短词列), 最后逐标的详情+完整票据。

### 报告图例

**三选二列** — 右侧确认的三个信号, ✓=当日成立 / ·=未成立:

| 缩写 | 信号 | 具体条件 |
|---|---|---|
| **低** | 不再新低 | 近 5 日最低价 > 再往前 15 日的最低价 (下跌动能衰竭) |
| **收** | 放量收复20日线 | 收盘站上 20 日线 + 近 10 日内曾在线下 + 量比 ≥ 1.5 |
| **破** | 突破20日高 | 收盘 > 前 20 日最高价 (下降趋势线突破的代理) |

≥2 个 ✓ **且**有真实回调前提 (近 15 日收盘曾低于 20 日线, 或距 60 日高
回撤 ≥8%) → CONFIRMED。纯阴涨趋势里单个 ✓ 常年亮着 (比如"低"), 回调
前提不满足就不算确认 — 所以概览表里一排"低✓ 收· 破·"的趋势股都不是信号。

**价值区列**: `380-440 (上方+14%)` = 接货带 380-440, 现价高于上沿 14%,
等回落; `(区内)` = 在接货带内, 可接货; `(破下沿-X%)` = 跌破下沿, 检查
论点是否失效。

**操作列词汇**:

| 词 | 含义 |
|---|---|
| `LEAP票👇` `CSP票👇` `spread票👇` | 已出票, 完整参数在下方详情 |
| `等财报后` | **右侧确认已成立、想出 LEAP, 但财报就在 ≤14 天内** — 拦的是财报**前**的窗口, crush 落地即解禁 (通常隔天), 不是"等 14 天"。财报在 15-30 天时照常出票, 只带一条"想避事件可等"的提示 |
| `LEAP被拦` `CSP被拦` | 想出票被其他过滤器拦 (年化不足/流动性/IVP), 原因见 ⏸ 行/详情 |
| `等回落入区` | 设了接货带但现价在上方 — 等价格回来, 不是"太贵不看" |
| `分批档👇` | 无期权链标的在接货带内 — 正股分批档位见详情 |
| `设区间` | 回调中但没设 value_zone — **设完后同一标的会变成**: 还在带上方→`等回落入区`; 跌进带→`CSP票👇`+正股分批档 (无链标的→`分批档👇`); 跌穿下沿→检查论点警告。不设则左侧引擎对它永远沉默 |
| `持有·跟20日线` `回踩中👀` | 右侧持仓姿态 |
| `等阶段2` | 右侧信号出现但倒挂未解除 |
| `别追·等回调` | 趋势里但无入场事件 — 空仓: 不追高, 等回调触发确认周期或回落入区; 持有: 继续拿 (要止损跟踪可手动把 state.json 设为 TREND) |

`等回落入区` vs `别追·等回调` 的分界 = **有没有设 value_zone**: 前者等的
是你定好的价格带 (落进带自动出 CSP+分批档, 左侧工具链); 后者等的是事件
(回调→三选二确认→LEAP, 右侧工具链)。给标的填上 value_zone, 它就从
"等事件"升级成"等价格"。
| `⚠️止损` | 右侧止损触发 (收盘跌破20日线) |

- `reports/YYYY-MM-DD-open.md` / `-close.md` + `latest-{open,close}.json`
- `data/state.json` — 每标的状态机 (只在尾盘扫描推进, 收盘口径)
- `data/iv_history.csv` — 每日 iv30/rv30 自建历史; 累计 60 个交易日后
  报告开始显示自建 IVP。在那之前 (以及之后) 以 moomoo IVP 实查为准。

## 状态机 (每标的, 收盘推进)

```
UPTREND ─┬─ 跌破20日线 → PULLBACK ─┬─ 进价值区 → LEFT_ZONE
         │                          └─ 三选二确认(有回调前提) → CONFIRMED
         └────────────────────────────────────────────┐
CONFIRMED → TREND (跟踪20日线) → 收盘跌破20日线 → PULLBACK (止损提示)
```

### 状态判定表 (7 个状态, 自上而下取第一个命中)

| 状态 | 报告显示 | 判定条件 (收盘口径) | 产出 |
|---|---|---|---|
| **TREND** | 右侧持仓(跟踪20日线) | 昨日为 CONFIRMED/TREND 且收盘仍 ≥ 20日线 — 右侧有粘性, 不会被价值区/回调抢走 | 跟踪 20 日线止损; 首次回踩不破 → **call spread 票** (3-6mo, 0.60/0.30 delta); 满 30 天 → 2x ETF/PMCC 工具切换提示 |
| **→ 止损** | (转 PULLBACK + 提示) | 昨日为 CONFIRMED/TREND 且收盘 < 20日线 | "右侧止损触发"提示 (凸性档减半/结构破清仓) |
| **CONFIRMED** | 右侧确认 | 有回调前提 (近15日收盘曾低于20日线, 或距60日高回撤 ≥8%) **且**三选二 ≥2 项: ① 不再新低 (近5日最低 > 前15日最低) ② 放量收复20日线 (收盘>20日线 + 近10日曾在线下 + 量比 ≥1.5) ③ 突破前20日高 | LEAP 票 (仅 NORMAL regime; 财报 ≤14 天或自建IVP >60 会拦) |
| **LEFT_ZONE** | 价值区内(左侧) | **收盘 < 20日线** 且 ≤ 区间上沿 — 左侧=买弱势, 趋势上方穿区不算; 跌破下沿另加"检查论点是否失效"提示 | CSP 票 |
| **NEAR_ZONE** | 接近价值区 | 收盘 < 20日线, 且在区间上沿之上 5% 以内 (`near_zone_pct`) | CSP 票 |
| **PULLBACK** | 回调中(20日线下) | 收盘 < 20日线, 离价值区还远 (或未设区) | 无票 (恐慌期 STAGE1 且设了 zone 例外) |
| **UPTREND** | 趋势上方 | 收盘 ≥ 20日线且无新确认 | 通常无; 价格仍在/近价值区时照出 CSP (接货限价单与趋势方向无关) |
| **NO_DATA** | 数据不足 | 日线历史 < 25 根 | 无 |

25-60 根日线 = **降级模式** (Yahoo 数据起点晚的新标的, 如 SPCX 2026-06
上市): 价格/20日线/量比/价值区照常, 右侧确认关闭 (回调前提需 61 根),
详情段带明确标注。

状态只在**尾盘扫描**推进并写入 `data/state.json` (开盘扫描只读不写);
"→ 止损"是 TREND→PULLBACK 的转换提示, 不是独立状态。另有一个
**回踩提示** (也不是状态): TREND 中当日下探 20 日线但收盘守住 →
"首次回踩不破 — 剧本首选入场/加仓点 (3-6个月 call spread)", 每轮
确认只提示一次 (state.json `retested` 去重, 新确认重新计数)。

Regime 覆盖 (优先于状态):

- **STAGE1 (倒挂)**: 已设 zone 的标的**不论状态**都出恐慌档 CSP (周权+16法则);
  右侧确认只记信号、不出 LEAP。
- **STAGE2_WINDOW (解除窗口)**: **不看状态**, 价格二选一 (收上20日线 /
  不再新低) 即补发 LEAP, 每窗口每标的一次 (state.json 里以 episode
  结束日去重)。

**CSP 触发与状态标签解耦**: 只看"价格是否在/近价值区"(+恐慌期例外),
不看趋势方向 — 在接货价挂收钱限价单, 涨着穿区也出票。**状态标签只说
趋势结构的实话**。冷启动提示: 扫描器只认自己见过的确认事件, 想让已
持仓的右侧标的直接进入 TREND 跟踪止损, 手动把 `data/state.json` 里该
标的的 `state` 改成 `"TREND"` 即可。

- **价格在/近价值区 (不论状态, 且已设 value_zone)** → 出 CSP 票:
  常规 12-31 DTE、delta 0.10-0.15; 恐慌期(倒挂)切周权+16法则距离
  (距离用所卖周权链自身 IV, 不用会低估的 30 天口径); 跨财报的到期日
  直接剔除。**行权价 ≤ 接货带上沿是硬约束** (不是警告), 且**年化 <
  `csp_min_annualized` (默认 10%, moomoo 筛选器同口径) 或权利金 < 0.20
  直接不出票**并说明原因 — IV 低/距离远时卖方三需求不齐, 剧本动作是
  正股限价单或等 IV, 不是硬卖。**没设价值区 = 没有接货价 = 不出 CSP 票**
  (ORCL 教训)。出场提示分档: 接货档拿到到期; 否则 GTC 三角。
  同时给出**正股分批档位** (① 带上沿 ② 带下沿 ③ 恐慌档 = 下沿再打
  18% 折扣, 间距递增) — IV 太薄不值得卖 put 时左侧的替代动作。
- **CONFIRMED** (新确认, 正常 regime) → 出 LEAP 票: 450-1100 DTE 优先
  1 月周期, deep ITM (指数 0.70-0.80 / 个股 0.75-0.85 delta), OI≥500、
  价差≤5% mid、外在价值≤40%; **财报 ≤14 天不出票** (crush 落地即解禁),
  15-30 天出票带提示; 自建 IVP>60 时改建议 spread/PMCC。
- Regime 门控: 倒挂(阶段1)期间**只出卖方票**, 右侧信号出现也不出 LEAP;
  倒挂解除窗口(阶段2, 峰值≥1.10 且 ≥3 日的倒挂在 10 个交易日内解除)
  是 LEAP 绿灯窗口 (buy the relief, not the panic) — 窗口内价格条件放宽为
  剧本的**二选一** (收上20日线 / 不再新低, 无量能要求), 且不依赖当日新
  确认: 历史典型序列是价格先确认、倒挂后解除, 每个解除窗口每标的补发一次
  (以 episode 结束日在 state.json 里去重)。
- Regime 数据: CBOE 官方日收盘 (Yahoo 的 ^VIX3M 会断更数周, 只作兜底) +
  盘中 15 分钟延迟临时点, 保证倒挂第一天当天就切换门控。

## Droplet 部署 + 邮件推送

无人值守方案 (替代本机 launchd, 不受电脑睡眠影响), 与 earnings-iv 的
droplet 模式同套路。Ubuntu 上:

```bash
# 1. 传代码 (rsync 或 git), 建环境
rsync -av --exclude .venv --exclude reports --exclude data \
    ~/Desktop/watchlist-scanner/ droplet:/opt/watchlist-scanner/
ssh droplet
cd /opt/watchlist-scanner
python3 -m venv .venv && .venv/bin/pip install yfinance pandas numpy
.venv/bin/python test_signals.py

# 2. 邮件配置: 复制模板并填 SMTP 凭据 (Gmail 用 App Password)
cp deploy/.env.example .env && chmod 600 .env && vi .env

# 3. 先手动验证一封
.venv/bin/python scanner.py --mode close --force --email

# 4. 挂 cron (服务器时钟 = UTC, 模板里已换算好双 DST 时点)
chmod +x deploy/run_scan.sh
crontab -e   # 粘贴 deploy/crontab.example 的内容, 路径按实际改
```

工作方式: cron 每个扫描各两个 UTC 时点 (夏令时/冬令时各一), scanner 的
美东窗口门自动选对的那个; 报告随 `--email` 推到你邮箱 (纯文本 markdown,
主题 `[watchlist] 日期 mode — 阶段`); 扫描失败邮 log 尾部, 22:30 UTC
看门狗发现当天报告缺失时邮 MISSED 报警。凭据全在 `.env` (chmod 600),
不进代码。macOS launchd 和 droplet cron 可以并存跑几天对比, 确认后
`launchctl unload` 本机的即可。

### 剧本工具阶梯覆盖图

| 周期阶段 | 剧本工具 | 扫描器产出 |
|---|---|---|
| 左侧·价值区 | 卖 CSP (接货价) | CSP 票 (年化≥10% 才出) |
| 左侧·IV 太薄 | 正股分批限价单 | 正股分批档位行 (①②③) |
| 拐点确认/阶段2 | deep ITM LEAP / risk reversal | LEAP 票 + RR 提法 |
| 突破后首次回踩 | 3-6 个月 call spread | call spread 票 (0.60/0.30 delta) |
| 趋势中段 (≥30天) | 2x ETF / PMCC 金字塔 | 工具切换提示 (per-ticker `two_x` 配置) |
| 趋势后期 | covered call + 移动止损 | 仅止损跟踪 — covered call 需要知道持仓, 超出扫描器边界 |

## 倒挂门控矩阵 (VIX/VIX3M 之外的加层, 2026-09)

依据 2026-09-02 倒挂指标研究 (VIX 系族横评 + 回测证据实查, 数字均经
独立源交叉验证)。硬门 (⛔) 以 skip_reason 呈现在被拦的票上, 预警 (⚠️)
只进"市场状态"节, 不拦票。**数据获取失败时门自动失效并在报告里明示** —
宁可漏也不靠坏数据硬拦。

| 门 | 数据 | 触发 | 动作 |
|---|---|---|---|
| VX 全曲线倒挂 | CFE 日结算 CSV (仅月度合约 — weekly 行价格是前月填充值, 已剔除) | 前 5 个月度合约逐对递减 | ⛔ 停开新 CSP + LEAP + 回踩 spread。依据: 2004 年以来 22 次全曲线倒挂, 21 次在 30 天内伴随 SPX >5% 回撤 (唯一漏网 2013 taper tantrum 只是局部倒挂)。比 VIX/VIX3M 多出的信号: 2024-08~11 曾出现 VX 期货倒挂而现货指数 contango 的背离。CSP 档想保留剧本恐慌档: `vx_full_backwardation_halt = false` (LEAP/spread 仍拦) |
| VX 局部倒挂 (M1>M2) | 同上 | M1 > M2 但未全曲线 | ⚠️ 预警: 前端承压, 关注是否蔓延 |



## 价值区间 (你的活)

`value_zone` 是"愿意接货"的价格带, 扫描器只算距离, 不替你估值。
PE-band 表和 sec 分析器的交易区间产出可以直接填进来。没设 zone 的标的
左侧建议不启用。当前预填: MSFT (PE带×FY27), AAPL (作者带, 已被击穿,
待更新)。

## 已知限制

- 尾盘扫描在 15:45 ET 跑, 当日 K 线还差 15 分钟收盘 — 信号口径视为
  "准收盘"; 量比用的是当日已成交量, 尾盘略低估。
- "突破下降趋势线"用 20 日高点突破做代理。
- 半日市 (感恩节次日等 13:00 ET 收盘) 尾盘扫描会被新鲜度门跳过。
- 电脑在触发时点**睡眠**: launchd 醒来只补一次触发, **关机**则直接丢 —
  10:30 看门狗会发 MISSED 通知, 但补不了已收盘的扫描; "MISSED" 也可能
  只是美股假日, 自行判断。
- 通知依赖 macOS 授权 (安装最后一步的测试通知务必点"允许")。run 日志按
  布里斯班日期命名、报告按美东日期命名 — 布里斯班周六早晨的失败写在
  周六的 log, 对应的是周五的报告。
- 财报日期来自 yfinance, 获取失败时票据会带"自查"提示 — 下单前照惯例
  核对 moomoo 财报日历。
- 自建 IVP 需要 60 个交易日积累; moomoo IVP (30天口径) 始终是主口径。
- 建议只做建议, 不碰下单。合约价是 mid 估算, 下单前实查盘口。
