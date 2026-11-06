# 主控驻场复核文件 — Hermes「死机/没反应」根因审计
> 用途：codex 审计期间的**已知事实基线**与**口径警告**。codex 可自读本文件并自我修正。
> 建立时间：2026-11-06 10:05 CST ｜ 主控：Hermes（小谷）

---

## §1 任务定义（用户原话）

> 「请用 Codex 全面仔细排查你为什么总是死机、没反应，找出根本原因，科学合理完整修复」

**判据（主控已实测确认的事实，可直接引用，但允许你推翻并给出更精确值）：**

| 编号 | 事实 | 主控实测命令/来源 |
|---|---|---|
| F1 | **磁盘 IO 饱和**：`sda`(ST9500325AS, ROTA=1 机械盘) `%util=89.40` `w/s=6096` `await=20.65ms` `aqu-sz=31.73` | `iostat -x 2 2` |
| F2 | **IO PSI 极高但内存 PSI 为 0**：`/proc/pressure/io` some avg300=46.31 full avg300=38.04；`/proc/pressure/memory` some/full avg10=avg60=avg300 全 0 | 内核 PSI |
| F3 | **内存不是瓶颈**：MemAvailable 4405MB，SwapTotal 7739MB / SwapFree 7739MB（swap 零使用），`vmstat` si=0 so=0 | `free -m` / `vmstat 1 3` |
| F4 | **假 load**：`loadavg 3.53/5.00/5.26` 但 `procs_running=2 procs_blocked=0` ⇒ 不是真负载 | `/proc/loadavg` vs `/proc/stat` |
| F5 | **D 状态线程卡在 ext4 日志提交**：`jbd2_log_wait_commit`(×2) / `folio_wait_bit_common`(×1) / `do_get_write_access`(×1) | 遍历 `/proc/*/task/*/stat` |
| F6 | **三个持续写盘者**（10 秒窗口实测 write_bytes 增量）：`main.mjs` pid=14835 **324KB/s**、`kline-store.mjs` pid=11371 **216KB/s**、`hermes gateway` pid=9028 **94KB/s** | `/proc/<pid>/io` 差分 |
| F7 | **hermes gateway 的 state.db = 772,325,376 B (736MB)**，`state.db-wal` = 13,810,272 B 且持续增长 | `ls -la ~/.hermes/state.db*` |
| F8 | **state.db 占用分布**（sqlite `dbstat`）：`messages` 328.40MB / `fts_v22_trash_messages_fts_trigram_data` **131.02MB** / `state_meta` **126.13MB** / `messages_fts_data` 44.07MB / `fts_v22_trash_messages_fts_data` 36.50MB / `messages_fts_trigram_data` 20.09MB / `system_prompts` 10.95MB，合计 735.17MB | `dbstat` 聚合 |
| F9 | **★ `state_meta.value` 存了 1~5MB 的单个 JSON blob**：`gateway.admission.result.v1.<hash>` 系列，每个 1.52~4.74MB，内容是 `{"_stored":{...},"result":{"api_calls":74,...}}`（完整 LLM 响应）。`state_meta` 仅 842 行却占 126.13MB（≈150KB/行），其中 `gateway.admission.result.v1.*` 合计约 100MB | `select key, length(value) from state_meta order by length desc` |
| F10 | **★ trash FTS 索引残留 167.5MB**：`fts_v22_trash_messages_fts_trigram_data` 131.02MB + `fts_v22_trash_messages_fts_data` 36.50MB；而**源表 `trash_messages` 已不存在**（`select count(*) from trash_messages` → ERR no such table）。⇒ 已删除消息的全文索引从未被回收 | `sqlite_master` + `dbstat` |
| F11 | **`messages` 111,140 行 / 328.40MB**（avg content 923B，max 101,868B，>10k 的 1680 条，>50k 的 113 条）；同时 `messages_fts` 111,114 行 + `messages_fts_trigram` 49,960 行 ⇒ 全文索引是第二份副本 | `dbstat` + `select count(*)` |
| F12 | **`state.db` 无碎片**：`page_count=188556` `freelist_count=6` `auto_vacuum=0` `journal_mode=wal` `wal_autocheckpoint=1000` ⇒ 736MB 是**活数据**，不是碎片堆积 | `pragma` |
| F13 | **存在 `state.db.auto-maintenance.lock` 与 `state.db.fts_rebuild.lock`** ⇒ 有 FTS 重建 / 自动维护机制 | `ls ~/.hermes/state.db*.lock` |
| F14 | **gateway 内存也在爬**：`gateway.log` 反复出现 `Agent cache pressure: anon RSS 1221MB over budget 800MB but no evictable session (1 cached, 1 mid-turn, 0 blocked on un-flushed persistence)`（11-03/11-04/11-06 多条） | `grep gateway.log` |
| F15 | **kline-store 本轮已跑 19 分 04 秒**（pid 11371，cron 每 30 分钟 :07/:37 一轮，技能记录正常单轮 455 币×3周期 ≈ 16 分钟）⇒ 有**跨轮重叠写同一批 store 文件**的风险（技能 `linux-swap-thrash-freeze-diagnosis` 记载过该半写竞争故障） | `ps -o etime` + 技能记录 |

---

## §2 主控已排除的方向（不要再重复排查）

```
✗ 内存不足 / swap 抖动  —— F3 已排除（swap 零使用，memory PSI 全 0）
✗ CPU 饱和              —— F4 已排除（load 假高，run/blocked 个位数，idle 80%+）
✗ 真实负载               —— F4 已排除
✗ 硬件故障（MCE/坏道）    —— 本轮未查，但 D 状态卡在 jbd2（软件路径）而非 ata 错误层 ⇒ 优先级低
```

---

## §3 口径警告（给 codex，避免你踩已知坑）

```
① ★ `state.db` 的 WAL 模式：读它时用 `file:...?mode=ro` URI，**不要**用 `set cipher='sqlcipher'`
   （那是 trading.db 的 SQLCipher, state.db 是普通 SQLite）
② ★ `state.db` 正在被 gateway 进程写：你的探针**必须只读**（mode=ro），不要在它上面跑 VACUUM/REINDEX
   ⇒ 你可以提出修复方案与**离线执行脚本**，但**执行时机由主控决定**
③ ★ `dbstat` 虚拟表在只读连接上可用，本轮已验证可用
④ ★ 时间戳：`messages.timestamp` 是 **Unix 秒(float)**；`state_meta` 的 key 里没有时间戳
⑤ ★ 主控报的「324KB/s」是 `write_bytes` 计数差分（含所有 fd 的写），**不等于**磁盘物理写速率；
   要拿物理速率请用 `/proc/<pid>/io` 的 `write_bytes` vs `iostat` 的 w/s 对照，或 `pidstat -d 1`
⑥ ★ 不要因为 state.db 有 736MB 就直接建议 VACUUM —— F12 已证明 freelist=6（无碎片），
   VACUUM 不会缩小文件，只会在机械盘上产生一次巨量重写（**更糟**）。真正的修复是**减少写入量**
⑦ ★ Hermes 主代码在 `/home/szjacky/.hermes/hermes-agent/`（**不是** smart_trading_v10）。
   smart_trading_v10 只是交易系统，它写盘（324KB/s）是**独立问题**，不要混进本轮结论
```

---

## §4 待裁决的问题（编号形式，含合格判据）

```
Q1 【admission result 大 blob】`gateway.admission.result.v1.*` 每个 1~5MB 存完整 LLM 响应。
   合格产出：找出写入点（文件:行号）+ 它为何必须存全量 + 可裁剪的字段清单
   + 裁剪后能否保住原语义（要能被 codex 自己证伪）。**若不能裁剪，给出替代存储形态**
   （例如 blob 落文件 + DB 存路径），并量化 IO 削减幅度。

Q2 【trash FTS 残留 167.5MB】源表已不存在，索引仍在。占用 DB 23%。
   合格产出：为什么 FTS 索引没随源表删除而清理（查 Hermes 的 trash/retention 路径）
   + 这 167.5MB 是否还在**持续产生写 IO**（还是只是静态占用）
   + 清理后能否真正释放（注意 F12：无碎片 ⇒ DROP 索引后文件不会缩小，需 VACUUM 或重建 DB —— 权衡机械盘代价）

Q3 【messages 328MB + FTS 副本 100MB】111,140 条消息，历史跨度 2026-04~2026-11。
   合格产出：现有 retention/清理机制是什么、为什么 111K 条消息没被清；
   若按"只保留最近 N 天"清理，会影响哪些功能（session_search / 上下文恢复 / 审计）
   ⇒ 必须给出**功能影响清单**，不能只说"清了省空间"

Q4 【state.db 写放大】736MB DB 上做 1~5MB 的行级 UPDATE ⇒ WAL 暴涨 + fsync + jbd2 提交阻塞。
   合格产出：量化「每次会话结束产生的字节数」与「WAL 增长速率」；
   给出能把 WAL/IO 降一个数量级的**结构性**方案（不是调 wal_autocheckpoint）
   ⇒ 依据 F12，`wal_autocheckpoint` 已默认 1000 页，问题不在 checkpoint 阈值

Q5 【gateway 内存爬升】F14 的 anon RSS 1221MB > 预算 800MB 且无会话可驱逐。
   合格产出：这与 Q1 的 4MB blob 是否有因果关系（blob 反序列化进内存 ⇒ RSS 涨）
   ⇒ 若是同一根因，合并结论；若独立，给出独立方案

Q6 【写盘源头排序】F6 三个写盘者。请给出**按物理写 IO**排序的完整清单
   （含 cron 批任务、journald、PM2 日志等），并明确区分：
   (a) 常驻持续写 (b) 周期性批任务 (c) 一次性大文件
   ⇒ 给出"砍掉/降频/改路径"后**预期 IO 削减百分比**

Q7 【可执行修复清单】把所有发现转成**有序可执行清单**，每项标注：
   风险等级(低/中/高) / 是否需要重启 gateway / 预计 IO 或内存削减量 / 一键复现或验证命令
   ⇒ **明确区分【可在不停机情况下做的】与【需要停机/重启的】**
   ⇒ 明确区分【改 Hermes 本体代码】与【改配置/运维动作】

Q8 【反证】你的结论里哪一条最可能是错的？给出最有力的反证。
   ★ 允许并鼓励你推翻主控的 F1~F15 每一个数字与判断 ——
     请在「独立复核节」逐条列出：你的数 / 主控的数 / 差异根因 / 证据。
   ★ 若某个数你复现不了，请穷举口径变体并说明哪个能对上；一个都对不上就直说"该数不可复现"。
```

---

## §5 交付格式要求（硬性）

```
1. **报告全文写入** `~/.hermes/hermes-agent/data/reports/_audit-freeze-20261106.md`
   （用 write_file 工具或脚本写盘）；stdout 只输出 ≤120 行摘要
   ⚠️ 理由：codex exec 的 stdout 有 ~400KB 上限，超限会丢报告
2. **数据产物落盘**：所有探针脚本放 `~/.hermes/hermes-agent/scripts/_audit_freeze_*.py|mjs`（**项目内**，不要放 /tmp ——
   /tmp 没有项目依赖）。原始观测（表行数/长度分布/dbstat 快照）落盘到
   `~/.hermes/hermes-agent/data/replay/_audit_freeze_*.json`
3. **按块推进、每块先落盘**：T1 取证 → T2 根因 → T3 方案 → T4 报告。
   每块做完先把自己的产物写盘再进下一块 ⇒ 即使 token 预算耗尽，前面的产物仍可用
4. **不要 git add -A / git add .** —— 显式文件列表；不要提交大文件（*.db / external/ / kline-store）
5. **不要改任何生产代码**（本轮只出方案 + 可执行脚本，不落地）。
   落地由主控在审核你的结论后决定
6. 复现脚本一律用项目内相对路径，便于 import 项目依赖
```

---

## §6 主控声明（不是结论，是我的判断，可能被你推翻）

```
我的主要假设（按可能性排序）：
H1 ★★★ state.db 里"每次会话写 1~5MB admission blob"是 IO 饱和的**主因**，
   因为它是唯一能把 736MB DB 上的单次写入放大到 MB 级、且频率与用户交互同频的机制
H2 ★★ trash FTS 残留 167.5MB 让**每次 WAL checkpoint 都要扫更大的索引树**，
   且 `messages_fts_trigram` 51,864 行使 FTS 写入本身变慢 ⇒ 放大 H1
H3 ★ kline-store 跨轮重叠 + 主链 324KB/s 常驻写 ⇒ 独立的 IO 贡献源（不属 Hermes 本体）
H4 ★ gateway anon RSS 爬升与 H1 同源（4MB blob 反序列化常驻）

我可能错的地方：
- F6 的 write_bytes 差分未必等于物理写 IO（见 §3⑤）
- admission blob 是否真的每次会话都重写（可能只在特定路径写）—— 需你从代码侧确认
- 736MB 的 DB 本身在机械盘上未必慢（顺序写可能很快），真正致命的是**随机大行 UPDATE + fsync**
```

---

## §7 已完成的前序工作（不要重做）

```
2026-11-04~11-06 交易系统侧（smart_trading_v10）已完成三轮裁决，与本任务无关：
  - 横截面空头通道证伪并关闭（R67 84配置 / R68 216配置，0 通过）
  - 主链零开仓裁决（16个 passedSymbols 死因全是"有实证依据的质量门"）
  - BTC 方向感知缺陷定位（n=1，按纪律未改）
**这些与 Hermes 本体的冻结问题无关，不要在报告里混入。**
```

---

## §8 主控第二轮补测（2026-11-06 10:30）—— ★ 新增两条 P0 ★

> 本节是主控在 codex 运行期间追加的。codex 若已读过本文件，请重读本节。

### ★★ P0-A 【外部看门狗 100% 失明】—— 「没反应」的第一直接机制

```
文件: /home/szjacky/.hermes/feishu-watchdog.py  (208 行)
单元: hermes-feishu-watchdog.service + .timer (每 ~60s 跑一次)

它的判定数据源 = journalctl --user -u hermes-gateway.service
它的 4 个匹配模式:
    DISCONNECT_PATS = ("receive message loop exit", "disconnected to wss://", "feishu disconnect timed out")
    CONNECT_PAT     = "connected to wss://"

★ 实测: journalctl --user -u hermes-gateway.service --since '-7d' (15685 行) 里
    "receive message loop exit"    命中 0
    "disconnected to wss://"       命中 0
    "feishu disconnect timed out"  命中 0
    "connected to wss://"          命中 0
    ⇒ last_disconnect 恒为 0.0 ⇒ broken 恒为 False ⇒ 【永远 verdict=ok (ws connected)】
⇒ ★★ 这个看门狗【从未真正工作过】(它守护的东西它一次都没看见)

而真实的断连/连接日志全部在【文件】~/.hermes/logs/gateway.log 里, 不是 journal:
    "[Feishu] Disconnected"                命中 173
    "[Feishu] Connected"                   命中  91
    "feishu connect timed out after 30s"   命中 198
    "feishu reconnected successfully"      命中  76
    "Reconnect feishu error"               命中  50
⇒ ★ 历史断连 173 次 / 自愈成功 76 次 / 【重连失败 50 次】⇒ 至少 50 次无人兜底

★ 为什么 journal 里没有这些行: unit 里 StandardOutput=journal, 但 journald 早期收了
  (2026-10-30 的 checkpoint_manager 报错还在), 之后 Hermes 侧把日志转到了文件 ⇒ 两边内容不重叠
```

### ★★ P0-B 【用户可感知的"失联窗口"实测 5 分 13 秒】

```
2026-11-06 09:40:54  ERROR feishu connect timed out after 30s
2026-11-06 09:40:54  INFO  [Feishu] Disconnected
2026-11-06 09:41:29  Reconnecting feishu (attempt 2)... → 09:42:01 又 Disconnected + 超时
2026-11-06 09:43:03  attempt 3 → 09:43:50 又 Disconnected + 超时
2026-11-06 09:45:53  attempt 4 → Connected ✅
⇒ ★★ 09:40:54 → 09:45:53 = 【5 分 13 秒用户完全发不出/收不到】= 这就是"没反应"
⇒ 且 watchdog 全程报 verdict=ok (ws connected) ⇒ 【无人知晓】

历史规模: 173 次 Disconnected, 76 次自愈成功 ⇒ 失败率约 29%
```

### ★★ P0-C 【内置 WS watchdog 也空转, 且它自己跑在被阻塞的 loop 上】

```
文件: plugins/platforms/feishu/adapter.py:4104 _ws_watchdog_loop
参数: _WS_WATCHDOG_LOOP_STRIKES = 2, interval 默认 (env HERMES_FEISHU_WS_WATCHDOG_INTERVAL)

★ 实测日志 (gateway.log):
    10:13:55 [Feishu] WS watchdog: ws loop consumed no frames in 60s; strike 1/2
    10:16:55 ... strike 1/2
    10:19:55 ... strike 1/2
⇒ ★★ strike 永远停在 1/2 ⇒ 每次都被重置 ⇒ 【永远达不到 2 ⇒ 永不触发 _ws_force_relink】
   机制: loop_strikes 只在"连续两次 frames 相同"时累加; 只要中间有一次 frames 变化就 reset 0
   ⇒ 【计数器语义缺陷】: 应按"最近 N 次采样中有 K 次无帧"或时间窗, 而非"连续"

★★ 更根本: 该 watchdog 是 async 协程, 跑在【gateway 主 event loop】上
   (代码注释自称 "lives on the ADAPTER loop, so gateway-level load cannot freeze it" —— 实际不成立)
   ⇒ ★ 主 event loop 被阻塞时, watchdog 自己也不跑 ⇒ 它【无法检测 event-loop 饥饿】
   ⇒ 而它要检测的正是 "event-loop starvation" ⇒ 自我盲区
```

### ★ 主控的修正（推翻自己先前的判断）

```
✗ 我先前说 "IO 饱和是主因" —— 【不成立】:
   实测 sda 写: w/s=4.00  wkB/s=8990  w_await=215.75ms  wareq-sz=2247.50KB
   ⇒ 是【少量超大字块写】(每次 2.2MB), 不是海量小 IO
   ⇒ 其中 8.9MB/s 里有 codex 自己写的 9007 KB/s (我引入的审计负载) ⇒ 瞬时值被污染
✗ 我先前把 sar -q 的列读错 (以为 blocked=721) —— 实际 blocked=15, plist=721
✗ 11-04 01:20-01:40 的 pswpin 22456/s 是【页/秒】= ~87MB/s, 且当时 swap 只用了 30%、
   blocked 只有 0~3 ⇒ 是【换页抖动】而非 swap 打满导致的整机假死
⇒ 修正后结论: 【"没反应" = 飞书 WS 断连 + 两个看门狗都失效】;
   【"死机" = 机械盘大块写延迟(215ms)导致的卡顿】; 两者是不同问题, 需要分别修
```

### ★ 新增问题（请 codex 一并回答）

```
Q9  【P0-A 的正确修法】
   → watchdog 应读哪个数据源? (gateway.log 文件 / journal / adapter 结构化健康接口)
   → 若读文件: 如何避免"文件被轮转/截断导致漏判"? (现 log-rotate.sh 每小时跑)
   → 判定应基于【断连事件】还是【健康心跳】? 哪个更抗假阳性?
   → 给出能覆盖【已发生的 50 次重连失败】的判定逻辑, 并给出可回放验证的日志样本

Q10 【P0-B 的失联窗口】
   → 173 次断连的【每次失联时长分布】是多少? (用 gateway.log 的 Disconnected/Connected 配对)
   → 网关自带重连退避 (60/120/240s) 的【失败次数】与【恢复时间分布】
   → 是否存在"重连成功但实际链路仍聋"(假恢复)? 怎么检测?

Q11 【P0-C 的计数器语义】
   → 把 "连续两次相同" 改成什么才既不误报也不漏报? 给出判据与参数
   → watchdog 跑在主 event loop 上的自我盲区怎么破?
     (候选: 独立线程/独立进程 探活 / 由 systemd 外部探针做 / 用 socket 层 TCP 状态)
   → 哪一个改动最小、最可靠? 给出实现位置(文件:行号)

Q12 【失联期间用户侧的可感知度】
   → gateway 在 WS 断连期间, 对已收到的消息会怎么处理? (排队? 丢弃? 报错?)
   → 有没有"断连时给用户一个明确提示"的机制? (用户视角: 至少知道"系统此刻收不到")
```

---

## §9 主控第三轮：★「没反应」的量化基线（验收锚点）★

> 这份数字是【修复效果的验收基准】。codex 若给出修复方案, 请说明它能把这个指标改善到多少。

### 失联窗口全量统计 (脚本: smart_trading_v10/scripts/_audit_feishu_disconnect_windows.py)

```
数据源: ~/.hermes/logs/gateway.log
配对规则: "[Feishu] Disconnected"  →  "[Feishu] Connected" 或 "feishu reconnected successfully"
事件: DOWN=173  UP=167
完整失联窗口: 86 个, 累计 387.0 分钟 (6.5 小时)

时长分布(秒): min=24  p25=84  中位=136  p75=299  p90=624  max=3658
  >=60s    68 个 (79.1%)
  >=120s   55 个 (64.0%)
  >=180s   34 个 (39.5%)
  >=300s   21 个 (24.4%)
  >=600s   11 个 (12.8%)
  >=1800s   1 个 (1.2%)

最坏 5 个:
  10-21 12:47:17 -> 13:48:15   61.0 min
  11-05 03:05:09 -> 03:21:30   16.3 min
  10-24 09:43:49 -> 09:59:15   15.4 min
  10-15 13:39:38 -> 13:54:25   14.8 min
  10-20 20:50:52 -> 21:05:11   14.3 min

按天(近期): 10-20 9次/31.9min | 10-21 10次/99.0min | 10-22 5次/33.3min | 10-23 7次/18.3min
今日 2026-11-06: 1 个, 09:40:54 -> 09:45:53 = 5.0 min
```

### ★★ 断连期间 gateway 的真实状态（决定「没反应」的性质）

```
2026-11-06 09:41:02,899 WARNING gateway.run: Gateway started with no connected platforms --
                                 1 platform(s) queued for retry: feishu: feishu connect timed out after 30s
2026-11-06 09:41:02,901 WARNING gateway.run: No adapter could be created for any of the 1 configured
                                 platform(s). ... Gateway will continue for cron job execution; platforms
                                 whose plugin may still register are queued for background retry.

=> ★★ 断连期间 gateway 是【无平台】状态:
   ① 用户消息【根本进不来】(不是排队等待, 是收不到) => 用户视角 = "发消息没反应"
   ② gateway 进程【活着】, cron 任务照常执行 => 机器没死, 是"聋了"
   ③ 恢复后立即正常: 09:45:53 Connected -> 09:48:04 收到用户新消息 => 无需重启
=> ★ 所以"死机没反应"的真相是: 【不是机器卡死, 是飞书通道断了且两个看门狗都看不见】
```

### ★ 给 codex 的第 13 个问题

```
Q13 【断连期间的消息语义】
   => gateway 在 "no connected platforms" 状态下:
     ① 用户从飞书发来的消息命运如何? (飞书服务端会重投吗? 还是丢弃?)
     ② 恢复后是否补发? (实测 09:45:53 连上, 09:48:04 收到的是新消息, 之前的没补)
     ③ 有没有【断连期间给用户明确提示】的机制?
        (用户至少该知道"你刚才那条我可能没收到, 请重发")
   => 这条是【用户体验层面】的修复: 让失联可见, 比重连更快解决"以为没反应"
```

---

## §10 主控第四轮：★★ event loop 冻结 + 内存护栏永久贴限（最严重的一环）★★

> 这是主控在 codex 运行期间追加的**最高优先级发现**，请优先回答 Q14/Q15。

### ★★★ P0-D 【gateway 内存护栏永久贴限 ⇒ 内核持续强制回收 ⇒ event loop 被冻住】

```
cgroup: /user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
  memory.current = 1,886,986,240 B   (1.76 GB)
  memory.high    = 1,887,436,800 B   (1.76 GB)
  ★★ 差值 = 450,560 B = 0.024%  ⇒ 【100% 贴着软限跑】
  memory.max     = 2,306,867,200 B   (2.15 GB)
  memory.peak    = 1,889,923,072 B
  memory.events  = low 0  high 14000  max 0  oom 0  oom_kill 0  oom_group_kill 0  sock_throttled 3138
  ★ high=14000 是【累计】计数; sock_throttled=3138 说明 socket 也被限速过

⇒ ★★ cgroup 长期贴着 MemoryHigh ⇒ 内核持续走强制 reclaim 路径
   ⇒ 热页反复换出换入 ⇒ 事件循环被切碎 ⇒ 进程被自己的内存限制反复冻住
⇒ ★★ 这与技能 devops/linux-swap-thrash-freeze-diagnosis 记载的【同款历史故障】完全一致:
   "hermes-gateway: current 2726203392B vs high 2726297600B → 仅差 94KB, events.high 累计 582366"
   ⇒ ★★ 【这个问题从 2026-10 就存在, 至今未修, high 计数仍在增长】

★ 当前实测还有一个 D 状态线程:
   ThreadPoolExecutor 线程卡在 jbd2_log_wait_commit  ⇒ 在等 ext4 日志提交
```

### ★★★ P0-E 【event loop 真的被冻结过 3 次 ⇒ 进程自杀重启 ⇒ 用户侧「没反应」】

```
grep "missed 3 consecutive liveness probes" gateway.log:
  2026-10-22 22:17:38,595 CRITICAL gateway.shutdown_watchdog: Gateway event loop missed 3 consecutive
                liveness probes; dumping all thread stacks and exiting with code 75 so the service
                supervisor can restart it.
  2026-10-23 01:29:04,886 CRITICAL 同上
  2026-10-23 09:57:59,193 CRITICAL 同上
  合计 3 次 (10-22 一次, 10-23 两次)

★★ ★★ ★★ 重启【没有救回飞书通道】—— 这是关键:
  10-22 22:17:38  event loop 冻结 → exit 75 → systemd 重启
  10-22 22:19:35  Starting Hermes Gateway...
  10-22 22:20:06  ERROR feishu connect timed out after 30s
  10-22 22:20:06  WARNING Gateway started with no connected platforms — 1 platform(s) queued for retry
  10-22 22:20:11  INFO  Starting reconnection watcher for 1 failed platform(s): feishu
  ⇒ ★ 重启只是把"冻住"变成"启动时连不上", 根因(内存/IO)未除 ⇒ 失联仍在
⇒ ⇒ 【所以"重启一下就好了"的直觉是错的, 必须修根因】
```

### ★★ 三条 P0 的因果链（主控综合判断，请 codex 独立复核/推翻）

```
[根因层]  memory.high 永久贴限 (current/high 差 0.024%) + 机械盘大块写 (w_await=215ms)
              ↓
[冻结层]  内核持续 reclaim ⇒ 事件循环被切碎
              ↓  (或: 直接 D 状态卡在 jbd2_log_wait_commit)
[表现层1] gateway.shutdown_watchdog 探活失败 ⇒ exit 75 自杀 (3 次, 10-22~10-23)
              ↓
[表现层2] 飞书 WS 心跳/帧消费停 ⇒ adapter 注销 ⇒ "no connected platforms"
              ↓
[表现层3] 用户消息进不来 ⇒ 用户视角 = "死机 / 没反应"
              ↓
[放大层]  两个看门狗都看不见:
           ① 外部 feishu-watchdog.py 读 journal, 而真实日志在文件 ⇒ 100% 失明
           ② 内置 _ws_watchdog_loop 的 loop_strikes 语义是"连续两次" ⇒ 永远停在 1/2
           ③ 内置 watchdog 自己就跑在会被饿死的主 event loop 上 ⇒ 自我盲区
```

### ★ 新增问题（最高优先级）

```
Q14 【内存护栏】(最高优先级)
   → memory.high 的当前值 1887436800 是怎么定的? (哪个配置文件/何时设定)
   → gateway 的稳定工作集 (memory.stat 的 anon+file) 实测是多少?
   → ★ 按"护栏必须显著高于工作集"的原则, 合理的 MemoryHigh/MemoryMax 应该是多少?
     给出计算依据: 当前工作集 + headroom, 并说明为什么
   → 能否【不靠加内存】而是先降工作集? 哪部分 anon 内存可以减?
     (候选: agent cache 的会话级缓存 / messages 载入 / 4MB admission blob 反序列化)
   → 给出一键验证: 设完 high 后 memory.current 必须稳定低于 high 且
     memory.events 的 high 【停止增长】(隔 60s 读两次相减)

Q15 【event loop 冻结】
   → shutdown_watchdog 的探活间隔/超时阈值是多少? 在哪 (文件:行号)?
   → 3 次冻结的【直接诱因】能从日志/堆栈 dump 里读出来吗?
     (日志里 "dumping all thread stacks" —— 那份 dump 在哪? 有没有留档?)
   → 冻结时正在跑什么? (大 tool 输出? DB 写? 外部 API 阻塞?)
   → ★★ 修复方向: 是"治冻结"(找出并避开阻塞调用) 还是"治后果"(缩短恢复时间)?
     给出【根治方案】: 哪个阻塞调用必须移出 event loop (给文件:行号)

Q16 【重启无效的连锁】
   → gateway 启动时飞书连接 30s 超时 ⇒ 直接进入"无平台"状态
   → ★ 为什么启动阶段不做【更长的连接重试/退避】?
   → 能否让 gateway 在飞书未连通前【不启动 cron/其他重活】, 优先保证通道先通?
   → 这样"重启"是否就能真正恢复? (这直接决定"要不要保留看门狗的重启动作")
```

---

## §11 主控已排除的方向（更新）

```
✗ CPU 饱和            —— %idle 常态 80%+
✗ swap 打满 / 内存耗尽 —— SwapFree 7739MB 全空闲, memory PSI 全 0
✗ 真实 load            —— procs_running=2 procs_blocked=0
✗ 硬件故障             —— D 状态卡在 jbd2 (软件路径), 非 ata 错误层
✗ "IO 饱和是主因"      —— 【主控已自我推翻】见 §8 末条: 实测是少量 2.2MB 大块写,
                          不是海量小 IO; 且当前 8.9MB/s 里大部分是 codex 自己写的(审计负载)
★ 保留: 机械盘大块写延迟 215ms 是【真问题】, 但它是【放大器】不是根因
```

---

## §12 主控第五轮：memory.stat 精确分解 + 「为什么又贴限」

### ★ 工作集分解 (实测, cgroup hermes-gateway.service)

```
memory.current = 1,757 MB → 1,785 MB  (60秒内涨 28MB)
memory.high    = 1,800 MB   ← 生效值 (与 10-memory-limit.conf 的 MemoryHigh=1800M 一致)
memory.max     = 2,200 MB
memory.peak    = 1,802 MB

memory.stat 分解:
  anon          = 545 MB    ← Python 进程的真实堆
  file          = 889 MB    ← ★ 几乎全是 state.db 的 page cache (DB 736MB)
  kernel        = 319 MB
  slab          = 315 MB
  inactive_file = 571 MB
  active_file   = 317 MB
  inactive_anon = 371 MB
  active_anon   = 176 MB

★ 关键换算: 非 anon 部分 (file+kernel+slab) = 1,523 MB  vs anon 只有 545 MB
★ ⇒ 【真正吃掉护栏的不是 Python 堆, 是 state.db 的 page cache + 内核/slab】
```

### ★ 为什么 2026-10-27 那次修复"又失效"了

```
10-memory-limit.conf 注释记录(10-27):
  实测工作集 anon 547M + file 536M(state.db page cache) + kernel 122M = 1.21G
  MemoryHigh 1250M 与之相差仅 40M -> memory.events high 累计 582366 次
  措施: high 1800M / max 2200M, 让 512MB 的 state.db 缓存能常驻

★ 现在: file 从 536M → 889M (+353M!), kernel 122M → 319M (+197M), slab 315M
  ⇒ 理由完全成立(想让 512MB 的 DB 缓存常驻), 但 DB 已长到 736MB, kernel/slab 也涨了
  ⇒ 工作集再次超过 1800M ⇒ 【再次贴限】
⇒ ★★ 这说明: 【只抬护栏是治标】, 必须同时做 Q1/Q2/Q3 的"减少数据量"才能根治
⇒ ★★ Q2 的 trash FTS 167.5MB 若清掉, file 立刻少 167MB ⇒ 直接腾出空间
```

### ★ 主控的自我修正（重要，避免 codex 顺着错误前提走）

```
✗ 我先前说 "贴限 ⇒ 正在抖" —— 【不成立】
  60 秒实测: memory.events high 14046 → 14046 (Δ=0), current 1757→1785MB
  ⇒ 按技能纪律(counters 是累计量不是现状), 【此刻没有在抖】
⇒ 正确表述: 【护栏设计已经【永久贴限】(长期无 headroom), 一旦工作集再涨一点就会开始抖】
⇒ ⇒ 修复判据必须是: current 稳定低于 high 【且有明确 headroom】, 而不是只解决"当前在抖"

★ 另一个此前漏掉的事实:
  cgroup 里除主进程外还住着:
    12663  graphify-mcp (MCP server, 跑 smart_trading_v10 的图谱)
    12664  process_supervisor.sh
  ⇒ ★ cgroup 是【共享池】⇒ 给 gateway 算 MemoryHigh 时要把 graphify-mcp 的占用算进去
    (否则抬护栏会连带放宽 graphify 的上限)
```

### ★ 请 codex 额外回答

```
Q17 【护栏的最终定值】
   → 考虑 cgroup 是共享池 (gateway + graphify-mcp + supervisor), 请给出:
     ① 各成员当前 RSS / anon / file
     ② 建议的 MemoryHigh / MemoryMax / MemorySwapMax (含计算依据)
     ③ 若同时做 Q1/Q2/Q3 (admission blob 瘦身 + trash FTS 清理 + retention 修好),
        工作集能降到多少? 护栏应定在多少才【有 headroom 而不是贴限】?
   ★ 验收判据: 设完 60 秒内 current 稳定低于 high 且【余量 > 15%】

Q18 【state.db 的 page cache 能不能不占 cgroup 预算】
   → SQLite 有没有手段把 DB 放在【另一个 cgroup/另一个盘】?
     (例如 sqlite3 的 file: URI + ATTACH 一个位于其它目录的库)
   → 或者: 是否该把 state.db 挪到 SSD / tmpfs / 或另一块盘?
     (本机只有一块 sda 机械盘 + zram; 检查是否有其他块设备可用)
   → 给出 2-3 个方案 + 各自的 IO/内存/风险代价

Q19 【kernel 319MB + slab 315MB 是谁的】
   → 这部分通常来自 socket 缓冲 / dentry / 网络连接数 / 线程栈
   → gateway 有 68 个线程 (nlwp=68) ⇒ 每个线程栈 8MB 上限, 实际占用多少?
   → 是否存在连接/线程泄漏? 给出实测命令与判据
```

---

## §13 主控第六轮：★★★ 现成的健康信号清单（外部 watchdog 全部没用上）★★★

### ★ 已有 4 个结构化健康源，外部 watchdog 一个都没用

```
① ~/.hermes/state/gateway.heartbeat          (294 B, 每 30s 刷新)
   {"pid":9028,"updated_at":"2026-11-06T02:38:02+00:00","monotonic":3889.5,
    "start_time":1793929128.19,"mem":{"rss_kib":552..., "mem_available_kib":4293568,
    "swap_used_kib":162948},"loop_tick_socket":true,"loop_tick_tcp_port":null}
   ★ 代码注释 (gateway/run_startup.py:894):
     "Start the loop-liveness heartbeat task ... An asyncio task so a frozen loop stops
      refreshing state/gateway.heartbeat"  (#66892)
   ★★ 【这就是 event-loop 冻结的专用探针】—— 主控实测它会停更 (24秒未刷新)

② ~/.hermes/state/gateway.loop-tick.<pid>.sock   (Unix socket, PID 后缀)
   ★ gateway/shutdown_watchdog.py:194-203 —— 专门用于 loop-tick 探活的 socket
   ★ 它就是 shutdown_watchdog 判定"event loop 冻结"的依据

③ ~/.hermes/state/gateway.lifecycle.json
   {"phase":"running","pid":9028,"start_time":...,"prior_unclean_exit":true}
   ★ prior_unclean_exit=true ⇒ 上次是非正常退出

④ gateway 内部已有 shutdown_watchdog (探活失败 → exit 75)
   实测触发 3 次: 2026-10-22 22:17:38 / 2026-10-23 01:29:04 / 2026-10-23 09:57:59
   ★ 它是【进程内】的 ⇒ 进程被冻住时它自己也不跑 ⇒ 只能抓"探活连续失败"
   ★★ 它的存在说明: 【冻结是被检测过的, 只是恢复手段(重启)治不了根因】

★⇒ 外部 watchdog 完全可以不 grep 日志, 改成:
   判据 = gateway.heartbeat 的 updated_at/monotonic 是否在 N 秒内刷新
        + gateway.loop-tick.<pid>.sock 是否存在且可连
   ⇒ 这比 grep 日志抗假阳性, 且 log-rotate 不影响它
```

### ★ 三个 watchdog 的现状（全部实测）

```
hermes-feishu-watchdog.timer  每1分钟   active/waiting
  → verdict=ok (ws connected)  【永远】, 因为读 journal 而模式全不匹配  ⇒ 100% 失明
mem-pressure-watchdog.timer   每5分钟   active/waiting
  → 只管内存/swap 压力 ⇒ 本次失联时 swap 零使用 ⇒ 【不会触发, 合理】
内置 _ws_watchdog_loop (adapter.py:4104)
  → strike 永远 1/2 ⇒ 【空转】
```

### ★ 一次失联的完整时序（2026-11-06, 可作为回归测试用例）

```
09:40:01  gateway 启动 (lifecycle.json started_at 01:40:01Z = 09:40:01 CST)
09:40:54  ✗ feishu connect timed out after 30s
09:40:54  [Feishu] Disconnected
09:41:02  WARNING Gateway started with no connected platforms — 1 platform(s) queued for retry
09:41:02  WARNING No adapter could be created for any of the 1 configured platform(s)
09:41:02  ⚠ state/gateway.loop-tick.9028.sock 创建于 09:41
09:41:08  INFO  Starting reconnection watcher for 1 failed platform(s): feishu
09:41:29  Reconnecting feishu (attempt 2)... → 09:42:01 又 Disconnected + 超时
09:43:03  attempt 3 → 09:43:50 又 Disconnected + 超时
09:45:53  attempt 4 → [Feishu] Connected in websocket mode (feishu) ✅
09:45:53  ✓ feishu reconnected successfully
09:48:04  收到用户"在吗"  ⇒ 恢复后立即正常, 无需重启
⇒ ★★ 用户侧失联 = 09:40:54 → 09:45:53 = 5 分 13 秒
⇒ ★★ 期间: gateway 活着(heartbeat 持续刷新)、cron 照跑、飞书通道全聋
⇒ ★★ 且这5分钟里 watchdog 每分钟报一次 "verdict=ok (ws connected)"
```

### ★ 请 codex 把 P0-A 的修法定在这套健康信号上

```
Q20 【用 heartbeat 替代 grep 日志】
   → 外部 feishu-watchdog.py 改成读 gateway.heartbeat 的 updated_at/monotonic:
     ① 判据: heartbeat 多久没刷新 ⇒ 冻结? (给阈值与依据)
     ② 失联(WS 断) 与 冻结(loop 卡) 是【两个不同的病】, 分别怎么判?
        - 冻结: heartbeat 停更       (loop-tick socket 也在)
        - WS 断: heartbeat 正常但 adapter 注销 ⇒ 需要一个"平台连接状态"的结构化信号
     → ★ 飞书侧有没有结构化的连接状态可读?
       (adapter 的 _write_runtime_status_safe 写到哪? 那个文件能读吗?)
   → gateway.heartbeat 在 gateway 重启后是否会短暂不刷新? (启动期宽限期怎么给)

Q21 【loop-tick socket 的可用性】
   → 它是怎么被 shutdown_watchdog 使用的? (读 gateway/shutdown_watchdog.py:180-340)
   → 外部进程能不能安全地连它做探活? (不会干扰 gateway 吗?)
   → 如果能 ⇒ 外部 watchdog 可以做到"冻结即知", 而不只是靠 exit 75 后知后觉
```


---

## §14 主控第七轮（最后一条补充）：★ 失联期间的用户消息【永久丢失】★

```
全量 86 个失联窗口, 窗口【内】到达 gateway 的用户消息 = 【0 条】
⇒ ★ 飞书服务端【不重投】断连期间的消息 ⇒ 用户发的那条【永久丢失, 无法恢复】
⇒ 窗口结束后 30 秒内才有用户消息到达 = 7 个 ⇒ 用户是在【重发】

★ 完整闭环 (用户视角):
  ① 用户发消息 → 飞书侧无连接 → 消息进了飞书的离线队列但【不会补投】
  ② gateway 侧什么也没收到 ⇒ 用户视角 = "我发了消息, 机器人死了"
  ③ 用户重发 → 若窗口已过则成功, 若还在窗口内则继续丢
  ④ gateway 日志显示一切正常, watchdog 报 verdict=ok
  ⇒ 【双方都困惑】: 用户觉得系统坏了, 系统觉得自己好好的
⇒ ★ 这解释了为什么用户说"总是死机"而不是"偶尔断线":
   因为断线期间的消息【无声消失】, 而系统从不告知
```

## §15 给 codex 的最后提醒（请收敛到写报告）

```
★ 你已跑 30+ 分钟, 已落盘 4 个数据产物 + 多个探针脚本, 【报告尚未落盘】
★ 请现在优先把报告写盘: data/reports/_audit-freeze-20261106.md
  (stdout 只输出 ≤120 行摘要 —— codex exec 的 stdout 有 400KB 上限, 超限会丢报告)
★ 报告必须逐条回答 Q1~Q21 (未答的明确写"未查"或"证据不足", 不要留空)
★ 并给出【可执行修复清单】: 每项标注 风险 / 是否需重启 / 预期收益 / 验证命令
★ 特别是 P0-A (外部看门狗失明) 和 P0-C (内置 strike 语义) 是【改配置或小改代码】即可,
  优先级最高, 请给出具体 diff 级方案
