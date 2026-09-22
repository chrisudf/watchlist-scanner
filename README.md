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
美东周五尾盘)、单次网络故障 (每组有重试时点) 都不用管。10:30 的看门狗按
`scanner.py` 里的 NYSE 日历算出当天美东交易日"本该有几份报告"(休市 0 份、
半日市 1 份、整日 2 份), 缺了才发 "MISSED" 通知 —— 假日和半日市不再误报,
所以收到通知就是真出事了 (多半是电脑睡眠/关机错过了触发时点)。

## 手动运行

```bash
.venv/bin/python scanner.py --mode close --force          # 立刻跑完整尾盘扫描
.venv/bin/python scanner.py --mode open --force           # 开盘异动
.venv/bin/python scanner.py --mode close --force --tickers NVDA,MSFT
.venv/bin/python scanner.py --mode close --force --no-options   # 只看技术面(快)
```

加 `--email` 会在写完报告后推送 (环境变量配置, 见 droplet 节; 邮件失败
exit 1)。两条通道: 设了 `SCAN_RESEND_API_KEY` 走 Resend 的 HTTPS API,
否则走 SMTP。**云主机上必须用 HTTPS 那条** —— DigitalOcean 等默认封锁
droplet 的出站 SMTP (25/465/587/2525 一律静默超时, 443 正常), Gmail SMTP
在那种机器上永远连不上。

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
| **LEFT_ZONE** | 价值区内(左侧) | **收盘 < 20日线** 且 ≤ 区间上沿 — 左侧=买弱势, 趋势上方穿区不算 | CSP 票 |
| **NEAR_ZONE** | 接近价值区 | 收盘 < 20日线, 且在区间上沿之上 5% 以内 (`near_zone_pct`) | CSP 票 |
| **PULLBACK** | 回调中(20日线下) | 收盘 < 20日线, 离价值区还远 (或未设区) | 无票 (恐慌期 STAGE1 且设了 zone 例外) |
| **UPTREND** | 趋势上方 | 收盘 ≥ 20日线且无新确认 | 通常无; 价格仍在/近价值区时照出 CSP (接货限价单与趋势方向无关) |
| **NO_DATA** | 数据不足 | 日线历史 < 25 根 | 无 |

**跌破区间下沿** (纯价格判定, 与状态标签解耦): 任何状态下收盘 < 下沿都
出"检查论点是否失效, 而不是继续摊"警告 — 包括反弹站上塌陷 20 日线的
UPTREND/TREND 和止损转 PULLBACK 当日; 破下沿期间铸出的 CSP 票第一条
note 带论点检查前置, "今日动作"的 🔵/🟢/🟣 真票行都带 ⚠️破下沿 前缀,
没有真票的标的 (分批档/被拦票) 以独立 ⚠️ 行出现在今日动作, 开盘警报
改说论点检查而非"核对接货挂单"。

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
  结束日去重)。同时 **CSP 常规档解锁** (已设 zone 的标的不论价格位置都
  试出票, 行权价仍卡接货带上沿) — 解除窗是统计最强卖权入场窗, 见下方
  倒挂门控矩阵。

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

# 2. 邮件配置: 复制模板并填 Resend api key (云上) 或 SMTP 凭据 (本机)
cp deploy/.env.example .env && chmod 600 .env && vi .env

# 3. 先手动验证一封
.venv/bin/python scanner.py --mode close --force --email

# 4. 挂 cron (服务器时钟 = UTC, 模板里已换算好双 DST 时点)
chmod +x deploy/run_scan.sh
crontab -e   # 粘贴 deploy/crontab.example 的内容, 路径按实际改
```

工作方式: cron 每个扫描各两个 UTC 时点 (夏令时/冬令时各一), scanner 的
美东窗口门自动选对的那个; 报告随 `--email` 推到你邮箱 (纯文本 markdown,
主题 `[watchlist] 日期 mode — 阶段`); 扫描失败邮 log 尾部, 看门狗按
NYSE 日历算出当天本该有几份报告, 缺了才邮 MISSED 报警 (判据与 macOS 那份
共用 `expected_report_modes()`)。凭据全在 `.env` (chmod 600),
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
独立源交叉验证)。硬门 (⛔) 以 skip_reason 呈现在被拦的票上 — 全市场
硬停牌在"今日动作"合并成一行 (标的列表 + 指路), 完整原因只在"市场状态"
的 ⛔ 行 (两道门消息不同时各一行); 预警 (⚠️) 只进"市场状态"节, 不拦票。
被 halt 拦下的一次性信号 (fresh_confirm 的 LEAP / 阶段2 leap_window /
回踩 retested) **不消耗其一次性标记** — halt 是暂态条件且 VX 结算滞后
一天, 解除后同周期内自动补发。**数据获取失败时门自动失效并在报告里
明示** — 宁可漏也不靠坏数据硬拦。

| 门 | 数据 | 触发 | 动作 |
|---|---|---|---|
| VX 全曲线倒挂 | CFE 日结算 CSV (仅月度合约 — weekly 行价格是前月填充值, 已剔除; 不足 5 个月度合约的盘中/假日 stub 视为未发布, 自动回看上一交易日) | 前 5 个月度合约逐对非升且至少一段真跌 (相邻平价 tie 是 feed 填充形态, 不降级) | ⛔ 停开新 CSP + LEAP + 回踩 spread。依据: 2004 年以来 22 次全曲线倒挂, 21 次在 30 天内伴随 SPX >5% 回撤 (唯一漏网 2013 taper tantrum 只是局部倒挂)。比 VIX/VIX3M 多出的信号: 2024-08~11 曾出现 VX 期货倒挂而现货指数 contango 的背离。CSP 档想保留剧本恐慌档: `vx_full_backwardation_halt = false` — **只在 STAGE1 生效**, LEAP/spread 仍拦; NORMAL/STAGE2 期照拦 CSP (全曲线倒挂可与 VIX/VIX3M NORMAL 并存, 那正是本门要拦的场景) |
| VX 局部倒挂 (M1>M2) | 同上 | M1 > M2 但未全曲线 (前端必须真倒挂 — 平价前端不算) | ⚠️ 预警: 前端承压, 关注是否蔓延 |
| 倒挂解除加成 | 既有 VIX/VIX3M ratio | 进入 STAGE2_WINDOW (达标 episode 解除后 10 个交易日内) | 🟢 CSP 常规档解锁 (不再要求价格在/近接货带)。依据: options.cafe 2009-2025 年 43 次倒挂事件 — 解除日买入 SPX 前瞻 5日 +3.04%/胜率88%, 21日 +4.38%/91%, 63日 +6.93%/88%, 全部碾压基线; 倒挂**开始**日反而无短期边际 (5日 -0.15%/51%, 74% 的 episode 继续跌) — 加成给解除不给开始, 与剧本 "buy the relief, not the panic" 同向 |
| VVIX 停牌线 | CBOE VVIX 日收盘 + 盘中临时点 (历史 CSV 冻结 >5 交易日且盘中点拿不到 = 按失败报, 门自动失效) | **NORMAL 期** VVIX ≥ 110 (`vvix_halt`) | ⛔ 停开新 CSP — 平静表面下 vol-of-vol 抢跑 = 对冲拥挤/裂缝先兆。只管 NORMAL: STAGE1 恐慌档与 STAGE2 解除窗 VVIX 高是常态, 剧本优先不加拦。参考档: <90 = VIX call/尾部对冲便宜 (囤凸性窗口), 90-110 中性, >140-150 = 恐慌对冲拥挤顶 (反而是左侧分批区) — 后两档只影响判断不进代码 |
| MOVE 背离 | Yahoo ^MOVE (无 CBOE 源, 断更 >5 交易日按失败报) | MOVE > 100 (`move_divergence`) 且 VIX < 18 (`move_calm_vix_max`) | ⚠️ 预警不拦票: 债券波动率先行于股票 (2023-03 SVB: MOVE 130→200 两天, VIX 晚数日; 2025-04 basis trade: MOVE ~172 先到) — 动作是缩短 put 名义、对冲前移, 人工判断 |
| 25Δ RR 倒挂 (每标的) | 该标的期权链 ~35 DTE (`rr_dte` 窗口), OTM 两侧各取 \|Δ\| 最接近 0.25 的行权价; **两腿 IV 一律从 bid/ask mid 反解** (Yahoo 预算 IV 列与 mid 反解差 ~2 pts 且方向恰在 put−call 上, 会把正常 skew 翻成假倒挂 — 2026-09-02 MSFT/GOOG 实测); **spot 用 put-call parity 反解的 expiry-specific forward**, 不用股票日线 (零股息 BS 会压低 call IV/抬高 put IV; 更要命的是日线本身可能陈旧 — 2026-09-04 实测 yfinance 日线返回 NaN 时会退回前一日收盘, 造出 11 个标的亮 10 个的假倒挂, 见 lesson.md); 报价须为 live bid/ask 且非 crossed、相对价差 ≤ `rr_max_rel_spread`、OI ≥ `rr_min_oi` | RR = put IV − call IV **< −1.0 pts** (`rr_invert_min_pts` 噪声地板 — 延迟报价有 ~1 pt run-to-run 漂移, 零阈值必出假旗标) | ⚠️ 每标的 froth 旗标, 例外才报告: CSP 对下行风险结构性少收钱 (行权价更远/更小/跳过); OTM/ATM LEAP 在付倒挂税 (只 deep ITM/正股 — 与既有 LEAP 过滤器同向); covered call/PMCC 短腿溢价异常肥 (只 covered 不裸卖)。背景: 2026-07 曾有 ~55% 的 SPX 成分股 1 月期 call skew 倒挂, 超过 2021 meme 峰值口径 — 挤仓追涨形态常见于局部顶 |



## 价值区间 (你的活)

`value_zone` 是"愿意接货"的价格带, 扫描器只算距离, 不替你估值。
PE-band 表和 sec 分析器的交易区间产出可以直接填进来。没设 zone 的标的
左侧建议不启用。当前预填: MSFT (PE带×FY27), AAPL (作者带, 已被击穿,
待更新)。

### zone 腐烂检测 (扫描器替你盯的部分)

手工 zone 会烂 (2026-09 校准实证: 4/15 旧锚三周~一个月内作废)。每个
zone 建议同时填 `zone_asof = 2026-09-05` (校准日期); 收盘扫描会盯:

- **超龄**: zone_asof 距今 > 60 天 → 复核提醒 (周频)
- **上沿漂移**: 现价距上沿 > max(15%, 校准日距离+8pts) 连续 10 个收盘
  → "zone 过时(偏低)" (自校准: 故意设深的带在校准日静默)
- **下沿升格**: 连续 10 收盘破下沿, 或单日深破 (下沿再 -10%) →
  单日"检查论点"升格为"zone 重锚"
- **拆股/合股**: zone_asof 之后发生 (无 zone_asof 时看最近 30 天) →
  zone 直接作废 (⛔, 左侧工具全部停用, 概览列改印"作废")。作废是
  **粘性**的 (记进 state.json, 事件滑出检测窗也不复活) — 只有重锚
  区间或更新 zone_asof 才解除
- **身份体检**: `kind = "etf"/"index"` 却查到真实财报日 → 提示复核
  (SPCX 案: ETF ticker 被普通股顶替 ~85 天无人发现)

监控状态存 `state.json` 的 `zone_watch` (区间数值或 zone_asof 一变即
归零重新观察); 提示首发 + 每 7 天重复, 不刷屏。阈值都在 `[settings]`
(`zone_asof_stale_days` / `zone_drift_*` / `zone_floor_instant_pct` /
`zone_flag_repeat_days`)。

## 推荐复盘 / 胜率 (`review.py`)

每次**自动**收盘扫描会把当天出的 CSP / LEAP 真票追加进
`data/recommendations.jsonl`, 供日后复盘。`--force` / `--tickers` 的手工跑
**不记** —— 与 `state.json` / `iv_history.csv` 同一条纪律: 手工重算是为了看
一眼, 记进去只会给胜率的分母灌水, 而且只灌在被手工跑过的那些天上。

```bash
.venv/Scripts/python.exe review.py                    # 结算 + 汇总
.venv/Scripts/python.exe review.py --backfill         # 从 reports/*-close*.md 反解历史
.venv/Scripts/python.exe review.py --symbol NVDA,GLD  # 只看某几只
.venv/Scripts/python.exe review.py --exclude-manual   # 剔掉手工跑的采样偏差
.venv/Scripts/python.exe review.py --json out.json    # 结算明细另存
.venv/Scripts/python.exe review.py --demo             # 造 mock 数据看报表长什么样
.venv/Scripts/python.exe review.py --md              # markdown 报表 -> reports/review-<日期>.md
.venv/Scripts/python.exe review.py --md out.md      # 指定路径
```

`--demo` 用**真实日线** + scanner 自己的 `bs_delta`/`bs_price` 造一份合成流水账
(写 `data/demo_journal.jsonl`, **不碰**真实账本; 显式 `--journal` 指向真账本会被
拒绝并 exit 2)。它解决的是"还没有数据时看不到报表长什么样"。

**免责声明跟着数据走, 不跟着命令走**: 任何一行 `source="mock"` 都会让
`summarize()` 无条件在报表顶部打印整块 `MOCK_CAVEATS`。理由很实际 —— 报表会被
截图、复制、隔几周再翻出来, 那时"这是 --demo 跑的"这个上下文早没了, 只剩一个
95% 的作废率。四条警告 (前视偏差 / IV 用 RV 代理 / 无盘口 / 固定周期入场) 必须
和数字绑在一起。

**为什么是 JSONL 不是 JSON 数组 / CSV**: 追加写不需要读-改-写整份文件 (中断
或并发时最坏少一行, 不会写坏历史); 每行独立可解析, 坏一行不影响其余,
`tail`/`grep`/`jq` 直接可用; 票据带 `notes` 列表, CSV 得拍平 (`iv_history.csv`
存纯标量, 那里 CSV 合适)。`pd.read_json(path, lines=True)` 一行读进 pandas。

去重键 = `date|symbol|kind|exp|strike`, 所以 DST 双发、看门狗补发、同一天重算
都不会把一张票记成两笔。

### CSP 和 LEAP 不合成一个胜率

**CSP** 有自然的二元结局 (到期那天要么在行权价上方作废、要么被行权), 到期
即可结算。**LEAP** 是 450-1100 DTE 的多头仓, 复盘窗口内**没有结局** —— 胜负
取决于你何时平仓, 那是持仓决策不是推荐决策。把未平仓的 LEAP 按当前浮盈算进
胜率, 等于拿"还没结束的比赛"的中场比分凑胜场数。所以报告分两块, LEAP 只给
未实现状态 (当前 ITM 比例 + 正股自推荐日涨跌) 并明确标注不计入胜率。

### CSP 的"胜"有两个口径, 都要看

- **① 作废率**: 到期收盘 > 行权价, 权利金全收。这是机械胜率。
- **② 越过盈亏平衡率**: 到期收盘 > 行权价 − 权利金。

被行权**不等于**亏 —— 这套剧本的 CSP 行权价本来就压在"愿意接货"的价值区
里, 接到货是预期内结果。只报 ① 会把"按计划接货"记成失败; 只报 ② 会掩盖接货
频率。另给"持有期内曾跌破行权价"的比例 (曾破位 ≠ 到期被行权) 和每股账面
结果 (= 权利金 + min(0, 到期收盘 − 行权价), 未计手续费与资金占用)。

### 邮件里直接读 (HTML)

不用另做渲染: `run_scan.sh review` 走的 `send_email_report` 两条通道 (Resend /
SMTP) **本来就在调 `scanner.md_to_email_html()`** —— 日报为此写的那套 (宽屏
`<table>` + 窄屏卡片降级), 复盘 md 原样复用, 在 Gmail 里就是排好的表格。

两个约束是实测出来的:

- **不能用 `_斜体_`**: `md_to_email_html` 的 markdown 子集不含它, 下划线会
  原样漏进正文。粗体/行内代码/引用块/表格/h1-h3 都支持。有测试逐份渲染后
  正则查孤立下划线。
- **单表最多 `MAX_TABLE_ROWS`(20) 行**: Gmail 正文超过 **102,400 字节**会被截成
  "[Message clipped]"。而为了手机可读, 每张表渲染两遍 (宽表 + 卡片), 实测
  **每行约 2.2KB**。LEAP 仓位 450-1100 DTE 不到期、只增不减 —— 按每周 1-2 张估,
  半年就 ~36 张、HTML ~110KB, 必被截断。

  **截断发生在渲染层不如发生在数据层**: 所以 LEAP 表按"离回本还差多远"排序
  (✗ OTM 最前 → ITM 未回本 → 越过平衡最后), 截断时留下的永远是该看的那几张,
  并明说省了多少笔。完整列表在 `reports/review-*.md` 与 `--json` 里, 不受限。

### 在哪跑 / 多久跑一次

**流水账只在 droplet 上长。** 收盘扫描自动记录, 而 `--force`/`--tickers` 的
手工跑按纪律不记 —— 本机跑的全是手工跑, 所以本机的 `data/recommendations.jsonl`
基本是空的 (且 `data/` 已 gitignore, 不会同步)。

| | 命令 | 说明 |
|---|---|---|
| droplet (数据在这) | `/opt/watchlist-scanner/.venv/bin/python review.py --md` | 写 `reports/review-<日期>.md` |
| droplet (自动邮寄) | `deploy/run_scan.sh review` | 出 md 并推到邮箱, 失败也发信 |
| 本机 (看历史) | 先 `rsync droplet:/opt/watchlist-scanner/data/recommendations.jsonl data/` 再 `.venv/Scripts/python.exe review.py --md` | 不 rsync 的话只能看 `--demo` |
| 本机 (看报表长什么样) | `.venv/Scripts/python.exe review.py --demo --md out.md` | mock 数据, 顶部有免责声明 |

**月度, 不是周度。** CSP 是 12-31 DTE, 一笔要三周左右才结算; 按当前出票节奏
一周只多出一两笔已结算样本, 而 delta 基准线的标准误要约 156 笔才减半 ——
周度报表的数字变化基本是噪声, 读它只会养成盯短期胜率的习惯。
`deploy/crontab.example` 里有现成的月度行 (每月 1 号)。

⚠️ 那行按**服务器本地时钟**写。文件其余时点是 UTC 口径, 而实际那台 droplet
的时钟是 `Australia/Brisbane` —— 照抄 UTC 会全错 (见 lesson.md)。复盘这行对
时点不敏感 (不依赖市场开闭), 但挂之前还是先 `timedatectl` 确认。

**注意流水账记的是"系统推荐了什么", 不是"你实际做了什么"。** 胜率衡量的是
扫描器, 不是你的账户 —— 你没开的仓、提前平掉的仓、加减过仓的, 它都不知道。

### 两种输出

默认打纯文本到终端。`--md` 另出一份 markdown —— 扫描器的日报本来就是 markdown
(邮件推送 + 手机阅读), 这份复盘同一条路就能发出去; 而且明细表在 md 下是**真
表格**, GitHub / 邮件客户端 / 预览器都能渲染, 不依赖等宽字体。纯文本那版在手机
上一定会折行错位。

两个渲染器**同源**: 所有口径只在 `compute_stats()` 里算一次, `summarize()` 与
`summarize_md()` 只负责排版。让两个渲染器各自算一遍 = 必然漂移 (改了一处忘另
一处, 两份报表给出不同的胜率, 而且没人会同时看两份所以不会被发现)。有一条测试
断言同一批数据在两份报表里的关键计数逐字一致。

### 怎么读这些数字

**作废率单看没有意义。** put 的 |delta| 近似它到期 ITM 的概率, 所以卖 0.12
delta 本来就"应该"有约 88% 作废。报告里的 `★ delta 基准` 行给的是：理论作废率、
实际作废率、**超额**, 以及这个超额有几个标准误。

```
★ delta 基准: 均 delta 0.122 → 理论作废率 88%; 实际 95%, 差 +7.1%
  n=39, 标准误 5.2% → 1.4σ —— 在噪声范围内, **还不能说系统有 edge**
```

95% 看着很强, 但对着 88% 的基准只有 1.4σ —— 统计上什么都不是。反过来: 同样
95% 的作废率, 如果卖的是 0.05 delta, 那是**跑输**基准。

**每股金额不可跨标的相加。** NVDA 200 的票和 RKLB 50 的票占用的保证金差 4 倍。
CSP 抵押 ≈ 行权价 × 100, 所以看 `抵押金回报率 = pnl / 行权价`。年化当量那行
假设资金连续复用且始终有票可卖 —— 实际有空窗, 别当真实年化。

**样本要多少才够**: 报告会算"把误差压到一半需要约 N 笔"。当前节奏下这需要
以年计的积累, 所以早期看这份报告主要是**看有没有异常**(某只票反复被行权、
某个 stage 下集中亏), 不是看胜率数字。

### 在途 CSP 看缓冲

未到期的 CSP 单出一张表: 标的 / 入手 / 到期 / 剩余天数 / 行权价 / 权利金 /
盈亏平衡 / 最新价 / **缓冲** / 持有期最低 / 状态, 按**缓冲升序**(最危险的排最前)。

判据是**当前 delta**, 不是缓冲。缓冲不含时间与波动率 —— 5% 缓冲剩 2 天
≈ Δ0.05 (没事), 5% 缓冲剩 30 天的高波动票 ≈ Δ0.35 (该管理)。delta ≈ 到期 ITM
概率, 而剧本入场是 0.10-0.15, 翻到 0.30 就是尺度无关的"明显走反了", 也是通行
的 roll 触发线。

状态分档: `⚠ 已破行权价` / `⚠ Δ≥0.30 该管理` / `注意 Δ≥0.20` / `曾破位` /
`安全`。表里同时给 `Δ开仓` 与 `Δ当前` 两列, 排序按 Δ当前降序。

**Δ当前是模型值**: σ 取流水账里的合约 IV (scan 行有); 回填行没有 IV, 但报告
行的 `缓冲 X%` 能还原开仓现价 (`spot = 行权价/(1−缓冲)`), 再由开仓 delta
反解 σ。**假设 IV 不变** —— 空头 put 走反时 IV 通常上行, 所以这个 delta 偏低、
触发偏晚。宁可晚报不要早报, 但读的时候要知道方向。
算不出 delta 的行退回缓冲 5%, 状态带 `?` 标注 —— 判据来源要可见。

⚠️ `iv_from_delta()` 的一个反直觉处: **|delta| 对 σ 并不单调**。σ→∞ 时
d1→∞、N(d1)→1, OTM put 的 |delta| → 0, 所以它先升后降有个峰。实测
103.23/85P/23天 的峰只到 ~0.26。二分从下界出发收敛到**较低**那个根 (现实那
一支); 目标高于峰值时返回 `None` 而不是凑一个假 σ。
"曾破位"与"当前已破"分开: 中途探过行权价又拉回来的, 和现在正压在下面的,
是两种处境 —— 前者说明你的行权价选得偏激进, 后者是眼下要处理的事。

### CSP 看被行权的那几笔

作废的单子没什么可看 —— 权利金全收, 按设计发生。**被行权的才带信息**, 所以
CSP 段除了汇总还给一张被行权明细: 标的 / 入手 / 到期 / 行权价 / 权利金 /
盈亏平衡 / 到期收盘 / 落价幅度 / 持有期最低 / 每股结果, 按每股结果升序
(最该复盘的排最前)。

- **落价幅度** = 到期收盘 / 行权价 − 1: 擦边被行权还是被砸穿, 是两种事。
- **持有期最低** : 到期擦边被行权, 和中途暴跌 30% 再拉回来, 是两种完全不同的
  经历 —— 汇总里的一个计数把它们抹平了。
- **每股结果为正 = 被行权但仍不亏** (到期收盘仍在盈亏平衡之上), 那是预期内的
  接货不是失败。

### LEAP 的开仓旗标

报告行尾的 `BE 249.55 (+11.4%)` 让回填能反解当时现价 (`spot = BE / (1+pct)`),
所以 LEAP 的"入手价""正股涨跌""BE%入手"三列即使是回填数据也有值。

**BE%入手 = 到盈亏平衡点的百分比**, 正是 LEAP 筛选器的门槛口径。`旗标`列把
开仓时踩过的线标出来 (`LEAP_GATES`: OI ≥500 / 外在 ≤40% / BE ≤12%):

```
COHR  ⚠ OI97 外在47% BE+21%
TSM   ⚠ OI1 BE+16%
ISRG  ⚠ OI2 外在62% BE+21%
```

⚠️ **这些票当初是带着警告发出来的**: `scanner.leap_ticket()` 的
`pick = min(clean or rows, ...)` 在没有合约同时满足全部过滤时会**回落到全部
候选**, 只追加一条 note。那条 note 进了报告正文, 但进不了表格 —— 复盘里
一行 OI=1 的票看起来和一行 OI=3567 的一样干净。这一列就是补这个。

注意 `scanner` 的 `passes()` 里**没有 BE% 这一项** —— 到盈亏平衡点是筛选器
的规则, 扫描器算了也打印了, 但从不据此过滤。

### LEAP 看明细, 不看聚合

LEAP 段除了聚合数还给**逐笔明细**: 标的 / 入手日期 / 到期日 / 行权价 / 入手时
现价 / 权利金 / 盈亏平衡 / 最新价 / 正股涨跌 / 状态 / 剩余天数。LEAP 是要长期
持有并择机 roll 的仓位, "当前 ITM 17/30"读不出该动哪一张。

**ITM 不等于赚钱。** 多头 call 的盈亏平衡是 `行权价 + 权利金`, 不是行权价。
深度 ITM 的 LEAP 权利金本来就厚, 现价越过行权价只说明有内在价值。所以状态列
分三档: `✓ 越过平衡` / `ITM 未回本` / `✗ OTM`, 汇总里两个计数并排 ——
只报 ITM 会系统性高估这条腿。demo 数据实测 ITM 17/30 而越过盈亏平衡只有 9/30。

入手时现价取自 `spot_at_rec`, scan/mock 行有、回填行没有 (报告正文里没写),
那几行显示 `—`。

### 回填历史

`--backfill` 从 `reports/*-close*.md` 正文反解票据行。这是**有损**的 —— 报告
是给人读的不是结构化存档, 拿不到 zone/stage/当时现价, 所以标
`source="backfill"` 与 `source="scan"` 分开统计, 别把两种数据质量混成一回事。
`-manual` 报告里的票当时确实推荐过, 收进来但打 `run_type="manual"`, 汇总里
单列一行提示采样偏差, 可用 `--exclude-manual` 对照。

历史报告在 droplet 上 (本地 `reports/` 只有少数几份)。把它的 `reports/` 同步
过来再跑一次 `--backfill` 即可, 或者直接在 droplet 上跑。

## 已知限制

- 尾盘扫描在 15:45 ET 跑, 当日 K 线还差 15 分钟收盘 — 信号口径视为
  "准收盘"; 量比用的是当日已成交量, 尾盘略低估。
- "突破下降趋势线"用 20 日高点突破做代理。
- 半日市 (感恩节次日等 13:00 ET 收盘) 尾盘扫描会被新鲜度门跳过。
- 电脑在触发时点**睡眠**: launchd 醒来只补一次触发, **关机**则直接丢 —
  10:30 看门狗会发 MISSED 通知, 但补不了已收盘的扫描。
- 看门狗的 NYSE 日历显式列到 **2027 年底** (`NYSE_CALENDAR_THROUGH`),
  之后一律按整日交易算 —— 届时假日会重新开始误报, 补表即可。这个失败
  方向是故意的: 表过期只会让告警变吵, 不会让它变哑。
- 通知依赖 macOS 授权 (安装最后一步的测试通知务必点"允许")。run 日志按
  布里斯班日期命名、报告按美东日期命名 — 布里斯班周六早晨的失败写在
  周六的 log, 对应的是周五的报告。
- 财报日期来自 yfinance, 获取失败时票据会带"自查"提示 — 下单前照惯例
  核对 moomoo 财报日历。
- 自建 IVP 需要 60 个交易日积累; moomoo IVP (30天口径) 始终是主口径。
- MOVE 只有 Yahoo 源 (^MOVE), 会像 ^VIX3M 一样断更 — 最新点老于 5 个
  交易日时按获取失败处理 (门自动失效并在报告标注), 不拿旧数当读数。
- VX 结算 CSV 的 weekly 合约行带的是前月填充价 (2026-09-01 实测六个
  不同到期全印 17.2528), 曲线形态只用月度合约。
- 25Δ RR 用 Yahoo 延迟报价的 mid 反解 IV, 单日读数仍有 ~1 pt 漂移
- 股票日线与期权链来自不同 Yahoo 端点, 可能不同步。**日线 bar 的日期落后于期权链 `max(lastTradeDate)` 的日期 = 硬拦** (⏸ 停出该标的所有票): 收盘价、右侧状态与票据全都建立在过期价格上。判据用日期而非价差 —— 难借券的高借券费会让 forward 合法地低于现货数个百分点 (`F = S·e^((r-q-b)T)`), 价差判据分不开"陈旧"和"难借", 硬拦会永久误伤那些标的
- 日线**不**陈旧却仍有大幅 forward/现货背离 (超 `rr_spot_gap_warn` = 1.5%; 35 DTE 的正常持有成本约 0.3%) → ⚠️ 提示持有成本异常, 常见于难借券: 做空拥挤的信号, 且卖 put 的净收益会被借券成本侵蚀
  (旗标已设 −1.0 pts 噪声地板) — 倒挂旗标亮了先在
  moomoo/IBKR vol lab 实查 risk reversal 再行动; 链稀疏 (|Δ−0.25|>0.10
  无行权价) 时宁可无读数也不硬凑。
- 建议只做建议, 不碰下单。合约价是 mid 估算, 下单前实查盘口。
