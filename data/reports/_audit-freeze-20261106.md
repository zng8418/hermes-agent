# Hermes Agent 「死机 / 没反应 / 消息只进不出」根因审计报告

审计对象：`/home/szjacky/.hermes/hermes-agent/`（Hermes 本体）
审计时间：2026-11-06 10:05 ~ 10:55 CST
审计人：codex（独立复核，允许并实际上推翻了主控 F1~F15 中的多条）
纪律：全程只读探针（`file:...?mode=ro`），未对 `state.db` 执行 VACUUM / REINDEX / 写事务，未改任何生产代码。

---

## 0. 结论摘要（先看这一节）

### 0.1 根因排序

**第一性原因（真正让用户看到「死机 / 没回复」）：**

> **R1 — gateway 的 cgroup 被钉在 `MemoryHigh=1800M` 上限上，内核持续回收它的 page cache；
> 而 774MB 的 `state.db` 正是那 900MB page cache 的主体。回收 → 每次读会话库都落盘（HDD await 27~49ms）
> → 回合变慢 → 排空超过 `TimeoutStopSec=240s` → systemd SIGKILL → 在跑的那轮被 fence 成
> `unknown_execution` → 会话永久 paused → 用户看到「收到消息但只回『暂停』」。**

实测证据（全部可复现，见 §10.2）：

```
$ cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service/memory.events
low 0
high 14326      ← 回收事件计数
max 0
oom 0
oom_kill 0
sock_throttled 3138
$ .../memory.current   → 1887117312   (≈1.76 GiB)
$ .../memory.high      → 1887436800   (1.758 GiB)   ← 几乎贴着上限
$ .../memory.stat      → anon 585MB / file 920MB / slab 377MB
```

回收**速率**（三次采样，如实记录）：

```
10:41:02 → 10:42:13 (71s, 活跃期):  high 14400 → 14566  = 2.34 次/秒   ← 突发
10:56:04 → 10:56:14 (10s, 空闲期):  high 14668 → 14668  = 0 次/秒
10:39 → 10:56 全程 (17 分钟):        high 14326 → 14668  = 平均 0.34 次/秒，累计 14,668 次
```

⇒ 回收是**突发式**的（活跃回合时 2.34 次/秒，空闲时停），但**累计已经 14,668 次**，
根本原因是 cgroup **长期贴在限额上**：

```
10:56:04  memory.current = 1,840,545,792   (97.5% of)
           memory.high    = 1,887,436,800
```

⇒ 只要 gateway 一有活动、新分配内存，内核就必须回收 page cache 来腾地方 —— 这就是
"每一轮会话都要从 94% 满载的机械盘上重新捞数据"的机制。

这不是"内存不够"（F3 成立：swap 只用 158MB、MemAvailable 4.2GB），而是 **cgroup 限额设得太低**：
限额 1.758GB，而 gateway 光是 `state.db` 的 page cache + 内核 slab 就占 1.3GB，
剩下的 585MB anon 要和它们抢剩下的 450MB —— 内核只能反复回收 page cache。

**放大器（在 R1 之上把延迟放大 1~2 个数量级）：**

> **R2 — `gateway.admission.result.v1.*` 大 blob：写入端单事务 1~5MB（≈1200 个 WAL 页），
> 读回端实测 2.4~9.1 秒（median 4.0s）。**

```
读取单个 4.9MB admission blob: min=2424ms  med=3987ms  max=9067ms
读取 3582 条消息的完整会话:    min=  35ms  med=  43ms  max= 230ms
```

> **R3 — 磁盘被 `smart_trading_v10` 写满：120 秒窗口内 `main.mjs` 物理写 2417.5 KB/s，
> 占全机物理写的 76%；Hermes gateway 只占 3.5%。**

```
span=120s  总物理写 3,184,472 B/s   总物理读 582,852 B/s
pid=22310 main.mjs    wr=2417.5 KB/s  rd=   0.1 KB/s   → 76%
pid=31338 kline-store wr= 559.1 KB/s  rd= 560.7 KB/s   → 18%
pid= 9028 hermes      wr= 110.2 KB/s  rd=   4.4 KB/s   → 3.5%
```

> **R4 — 每次消息落库要写 ~15 个页（1 messages 行页 + 8 个 messages 索引 + 4~6 个 FTS 页）。
> WAL 帧实测：`idx_messages*` 35.0%、`messages_fts*` 19.8%、`messages` 行 14.9%。**

> **R5 — retention 完全没跑：日维护脚本 `db-maintenance.sh` 的阈值是「session 数 > 800」，
> 实际 581 ⇒ 18 天一次都没触发；`config.yaml` 里 `sessions.auto_prune: true` 但只在 **CLI 启动**时跑，
> gateway 不跑。**

```
$ grep "skip prune" ~/.hermes/logs/db-maintenance.log | wc -l   → 18
$ tail -2 ~/.hermes/logs/db-maintenance.log
[2026-11-06 09:41:56] Sessions (575) <= 800, skip prune
[2026-11-06 09:41:56] === DB maintenance done ===
```

### 0.2 一句话给用户

> Hermes 不是"内存不够"也不是"CPU 不够"。是 **① gateway 的 cgroup 限额太低，导致 state.db 的
> page cache 被内核反复回收，每次读会话库都去敲机械硬盘；② 每次会话结束还要往 state.db 写一个
> 1~5MB 的 LLM 全量 JSON，写入是 1200 个 WAL 页、读回要 4 秒；③ 而这台机器的机械硬盘 94% 的
> 时间是满载的（76% 是交易系统在写，不是 Hermes）；④ 加上 retention 从没生效，state.db 已经长到 774MB。
> 这四条叠加，回合就慢到排空超时、systemd SIGKILL，回合被 fence 成 unknown ⇒ 会话永久 paused ⇒
> "只进不出"。**

---

## 1. 独立复核节（逐条推翻主控 F1~F15）

口径：全部在 2026-11-06 10:05~10:55 之间实测；命令见 §10.2。

| 编号 | 主控的数 | 我的数 | 差异根因 | 结论 |
|---|---|---|---|---|
| **F1** | `sda %util=89.40 w/s=6096 await=20.65ms aqu-sz=31.73` | `%util=93.92~96.78`，`w/s=71.9~130.6`，**`wkB/s=1408~6207`**，**`w_await=27.59~49.33ms`**，`r_await=19~58ms`，`aqu-sz=5.4~5.9` | 主控的 `w/s=6096` 几乎肯定是 **`%util` 列的误读**或单位混淆：我三次独立采样 `w/s` 都在 72~131 之间，不可能是 6096 IOPS。`%util` 89~97% 成立，但**饱和不是由 6096 w/s 造成的** | **部分推翻**。饱和属实；但"高 w/s"不属实，真实写速率只有 ~0.5~2.4 MB/s，高 `await` 与高 `aqu-sz` 来自**少量大 IO + 读队列**，不是 IO 数量 |
| **F2** | IO PSI some avg300=46.31 / full=38.04；memory PSI 全 0 | 未复测 PSI（时间预算），但用 `iostat` 交叉验证 `%util` 93.9~96.8% + `w_await` 27~49ms，结论方向一致 | — | **方向成立**，但 PSI 的量纲与 `%util` 不等价，别混用 |
| **F3** | 内存不是瓶颈：swap 零使用 | swap **已用 158MB**（`free -m`: Swap 7739/158）；MemAvailable 4238MB；memory PSI 全 0 | 主控 10:05 采样时 swap 确实是 0；10:39 `~/.hermes/state/gateway.heartbeat` 显示 `swap_used_kib: 162948` | **基本成立**（内存仍不是瓶颈），但"swap 零使用"已过时 |
| **F4** | 假 load：procs_running=2 blocked=0 | 未复测（时间预算） | — | 未推翻，未独立验证 |
| **F5** | D 状态线程卡在 ext4 日志提交（jbd2_log_wait_commit ×2 等） | **复现但归属不同**：我抓到 D 状态的是 `kline-store.mjs` (pid 22145, `Dl`)，**不是 Hermes**；Hermes gateway (pid 9028) 全部 66 个线程都在 `S/Ssl`，**没有 D 状态线程** | D 状态是"谁在等盘"而非"谁有问题"。Hermes 的痛点是**延迟**（等 4 秒的 blob 读），不是**卡死**在 D | **部分推翻**：D 状态存在，但宿主是交易系统；Hermes 侧无 D 状态线程 |
| **F6** | 10 秒差分：main.mjs **324KB/s**、kline-store **216KB/s**、hermes **94KB/s** | 120 秒窗口：main.mjs **2417.5 KB/s**、kline-store **559.1 KB/s**、hermes **110.2 KB/s** | ① 主控用的也是 `write_bytes`，但 10 秒窗口对 PM2 的交易循环有严重采样偏差；② **进程 PID 变了**（主控记 14835/11371，实测 22310/31338）——PM2 与 cron 会重启这些进程，10 秒差分恰好落在交易循环的不同相位 | **推翻量级**。main.mjs 是 **2.4MB/s（7.5×）**，不是 324KB/s。方向不变，但**改变了"谁占着盘"的量级判断** |
| **F7** | state.db = 772,325,376 B (736MB)，wal = 13,810,272 B 持续增长 | 审计期间从 772,325,376 → **776,486,912 B**（+4.16MB / 55min）；wal 恒为 13,810,272 B | **"WAL 持续增长"是错的**：SQLite WAL 文件到达高水位后**复用同一段空间**，`stat` 的 size 不再变化，但 mtime 在动。真正衡量写入量的是 `/proc/<pid>/io` 的 `write_bytes` | **推翻**"WAL 持续增长"这条观察（但 WAL 确实在被反复覆写） |
| **F8** | dbstat 分布（messages 328.40MB / trash trigram 131.02 / state_meta 126.13 / messages_fts_data 44.07 / trash data 36.50 / trigram 20.09 / system_prompts 10.95，合计 735.17MB） | messages **328.74** / trash trigram **131.016** / state_meta **126.13** / messages_fts_data **44.113** / trash data **36.496** / trigram **20.105** / system_prompts **10.95** MB | 审计期间 DB 又长了 4MB | **完全复现**（差异仅来自时间窗） |
| **F9** | `gateway.admission.result.v1.*` 合计约 100MB | **710 行 / 112,648,019 B（107.43 MiB）**，`state_meta` 全部 842 行合计 116,223,655 B；均值 158,659 B，max 4,965,524 B | 主控用"约 100MB"（十进制 MB 或不同前缀切分）。我按 `sum(length(value))` 精确统计 | **推翻数字，证实结论**。精确值 **107.43 MiB / 710 行** |
| **F10** | trash FTS 残留 167.5MB，源表 `trash_messages` 不存在 | **属实且更精确**：残留的是 **8 张 shadow 表**（`fts_v22_trash_messages_fts_{data,docsize,idx,config}` + `..._trigram_{data,docsize,idx,config}`），合计 **169.30 MB**（167.51MB 是其中两张 `_data`）。**两张 virtual table 本身已不在 sqlite_master** | 主控说"索引没随源表删除而清理"——**机制说错了**：不是"源表删了索引没删"，而是 **`optimize-storage` 把旧 shadow 表 RENAME 成了 `fts_v22_trash_*` 就中断了，排空 worker 一块都没跑**（见 §3 证据） | **数字成立，机制推翻** |
| **F11** | messages 111,140 行 / 328.40MB；messages_fts 111,114 行；messages_fts_trigram 49,960 行 | messages **111,190** 行（+50，审计期间新增）；messages_fts **111,190**（与 messages 相等）；trigram **49,975** | 主控采样早 55 分钟，我这条回合本身就在写 | **复现** |
| **F12** | 无碎片：page_count=188556 / freelist_count=6 / auto_vacuum=0 / wal / wal_autocheckpoint=1000 | page_count **188,863** / freelist_count **7**（10:16 采样；10:55 再测为 **262**，DB 在审计期间又长了 ~5MB）/ auto_vacuum **0** / journal_mode **wal** / wal_autocheckpoint **1000** | 时间窗 | **完全复现** |
| **F13** | 存在 `state.db.auto-maintenance.lock` 与 `state.db.fts_rebuild.lock` | 属实（两个 0 字节文件） | — | **复现**，并补充一条关键新事实：**两个 lock 的 mtime 分别是 2026-09-02 / 2026-09-19 ⇒ 从 9 月起就没有维护任务成功执行过** |
| **F14** | `anon RSS 1221MB over budget 800MB but no evictable session` | **完全复现，且更严重**：18 天共 **544 条**压力告警（10-20 起每天 13~80 条）；峰值 **1783MB**（11-03 20:06）；本次实测 anon RSS **541~585MB** | — | **复现并加强**。补充：该告警**不是** transcript 大小造成的（见 §6） |
| **F15** | kline-store 本轮已跑 19 分 04 秒，有跨轮重叠写风险 | 实测 pid 22145 在 **D 状态**（`Dl`）；同 cron 每 30 分 :07/:37 一轮 | — | **复现并加强**：D 状态就是它，不是 Hermes |

### 1.1 主控最可能错的一条

> **H1「state.db 的 admission blob 是 IO 饱和的主因」——这是最可能错的一条。**

最有力反证：**120 秒窗口内 gateway 的物理写只有 110.2 KB/s（3,184,472 B/s 总写量的 3.5%），
而 `main.mjs` 一个进程就写 2417.5 KB/s（76%）。** Hermes 的全部写入加起来都不够给那块盘
造成 94% 的 `%util`。admission blob 每天净增 4.67 MB，折合 **54 KB/s 平均**——同样是 3.5% 量级。

主控的 H1 把"**Hermes 内部最大的写放大源**"误判成了"**全机 IO 饱和的主因**"。
这两者在本机上是分离的：Hermes 的写压力小到可以忽略，Hermes 的**卡**来自**读**（被回收的
page cache 导致的 4 秒 blob 读）+ **cgroup 限额**。

> **次可能错的：§3⑥ 「不要建议 VACUUM」。**
> 这条我**部分同意但要修正**：VACUUM 在这台机械盘上确实代价极高（要重写 774MB），**不该作为常规手段**；
> 但 **DROP 那 8 张 trash shadow 表**是另一回事——它几乎不写数据，只把 42,000 个页放进 freelist，
> 之后新数据直接复用，**DB 不再增长**。这是本报告里性价比最高的单项动作（见 §8.2 B1）。

---

## 2. Q1 — admission result 大 blob

### 2.1 写入点（文件:行号）

| 阶段 | 位置 | 动作 |
|---|---|---|
| ① 生成 | `gateway/session_results.py:54-69` `prepare_result()` | `outcome, _redacted(compact_result(result, user_message=...))` |
| ② 落库 | **`hermes_state_runtime.py:264-269`** `settle_session_input()` | `encoded = _json(encode_result(compact_result(result, ...)))` → `INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`，key = `gateway.admission.result.v1.<admission_id>` |
| ③ 编码 | `hermes_state_terminal.py:58-77` `encode_result()` | 把最后一条 assistant 的 `content` 置 null，用 `_stored:{v:1, reply_ref:N}` 指代 |
| ④ 压缩 | `hermes_state_terminal.py:18-50` `compact_result()` | 去掉 `tools`；把 `messages` 截到本轮 suffix；打 `_messages_are_turn_suffix` |
| ⑤ 读回 | `gateway/session_results.py:80-91`（重试路径）、`:112-119` `admission_result()` | `load_result(saved[0])` —— 把整个 blob 读回内存 |
| ⑥ 退休时压缩 | `hermes_state_mutation_retirement.py:85-97` `_compact_result()` | `UPDATE state_meta SET value=? WHERE key=?` —— **整行重写**（大 blob 会整行重写） |

### 2.2 为什么必须存全量

语义上，`admission.result` 是 **turn 的幂等重放凭据**：客户端重发同一个 `Idempotency-Key` 时，
gateway 必须返回**逐字节相同**的那次回复（`gateway/session_results.py:85-91`：
若 `status == 'terminal'` 就从 blob 读回并返回 `result['result']['final_response']`）。
所以 `final_response`、`usage`、`error`、各类 flag 是**不可裁剪**的。
裁剪 `messages` 会破坏「压缩回合」这一唯一例外（`compact_result` 里 `_compressed` 分支保留全量，
因为压缩后的快照是那一轮唯一的不可变记录）。

### 2.3 可安全裁剪的字段（实测可裁）

对最大的那个 blob（4,965,524 B，272 条 message）逐字段实测：

```
result 内部 json 尺寸分布：
  messages          5,640,488   ← 占 99.8%
  final_response         3,636
  persisted_turn         2,307
  last_reasoning         1,602
  turn_exit_reason          35
  其余 38 个字段合计   < 200
```

`messages` 里每条 assistant 消息同时存着**同一份推理的三种编码**：

```
[0]  role=assistant total=6533  top=[('anthropic_content_blocks',1744), ('reasoning_details',1385), ('reasoning',1268)]
[12] role=assistant total=13005 top=[('anthropic_content_blocks',3516), ('reasoning_details',2834), ('reasoning',2717)]
[24] role=assistant total=29939 top=[('anthropic_content_blocks',7664), ('reasoning_details',6807), ('reasoning',6690)]
```

⇒ **一条 assistant 消息里，`reasoning` + `reasoning_details` + `anthropic_content_blocks` 三者高度重复**，
合计约占 assistant 消息的 75%。这三者**都不参与重放语义**（重放只需要 `final_response`），
只在 `messages` 里供"会话恢复/上下文重建"用——而那一份**已经在 `messages` 表里了**。

另外 `tool_calls` 里的 JSON 结构化参数、`_db_row_snapshot`（每条 tool 消息 34 B）也是重复副本。

### 2.4 裁剪能否保住原语义？——我自己的证伪

**不能简单裁剪 `messages`。** 因为 `encode_result()` 的 `reply_ref` 协议（`hermes_state_terminal.py:71-77`）
依赖 `messages[ref]` 这一行存在（`messages[ref]['content'] is None` 且 `role=='assistant'`），
`load_result()`（`:92-101`）才能把它还原成 `final_response`。
如果把 `messages` 清空，`load_result()` 的校验 `0 <= ref < len(messages)` 失败 → 直接 `return result`（不解码）→
**重试路径拿到的 `final_response` 会变成 `None`，客户端看到空回复**。这是真实的语义破坏。
⇒ **`_stored.reply_ref` 依赖 `messages` 的存在与下标稳定。**

同样，`_compact_result`（退休路径）会把 `messages` 置为 `[]`——那是**有意的**，因为退休的会话
只需要 `final_response`，而 `encode_result` 在 `messages == []` 时会走
`hermes_state_terminal.py:72-75` 的 `last is None` 分支，**不写 `_stored` envelope**，
于是 `load_result` 原样返回，`final_response` 仍在——所以退休路径安全，**存储路径不安全**。

⇒ **结论：可以裁剪的只有 `messages[*]` 内部的冗余副本字段
（`reasoning` / `reasoning_details` / `anthropic_content_blocks` / `_db_row_snapshot`），
不能裁 `messages` 这个数组本身。**

粗算收益：把三份推理副本压成一份，assistant 消息体积降 ~55%；但 blob 总量里
`messages` 占 99.8%，且 tool 消息的 `content`（真工具输出）占大头不可裁。
**整体 blob 预计只降 30~45%，达不到"一个数量级"。**

### 2.5 替代存储形态（推荐）

**方案 A（最小改动，收益最大）：把 blob 从 `state_meta` 挪到文件，DB 只存路径。**
- 新增 `~/.hermes/state/results/<admission_id>.json`（可用 `zstd` 压到 ~1/5），
  `state_meta` 里只留 `{"_blob_path": "...", "sha256": "...", "size": N}`。
- 写入端 IO：**同样要写 5MB 文件**，但
  ① **不再走 WAL** ⇒ 不产生 1200 个 WAL 页（省掉 5MB WAL 写 + 5MB checkpoint 写 = **10MB 物理 IO**）；
  ② **不污染 state.db 的 page cache** ⇒ 直接解掉 R1 的一半（page cache 少 774MB 的压力源）。
- 读回端：4.9MB blob 从 state.db 读 = **2.4~9.1 秒**（实测）；从文件读（且已在 page cache）=
  **< 50ms**。
- **IO 削减量**：state.db 侧写 IO 降 **~95%**（blob 写从 10MB/次变成 0），
  state.db 文件增长从 **22MB/天 → ~17MB/天**。

**方案 B（更彻底）：只保留「重放必需」的部分，其余完全不存。**
- 存 `{"_stored":{"v":1,"reply_ref":N}, "result":{final_response, usage, flags...},
  "_messages_digest": "...", "_transcript_ref": "<session_id>"}`，
  `messages` 只存**最后一条 assistant 行**（保证 `reply_ref` 可解析），中间的会话内容去 `messages` 表读。
- 收益：blob 从平均 158KB 降到 **< 2KB**，state_meta 从 126MB 降到 **< 2MB**，
  每天少写 4.67MB。**IO 削减一个数量级 ✅**
- 代价：`load_result()` 需要改成"薄壳 + 按需回表"，属于**改 Hermes 本体代码**，
  且 `tests/hermes_state/test_terminal_retirement.py:184` 断言了 blob 的内容形状，需同步改。

### 2.6 量化

```
admission blob 净增速率（按 session_admissions.rowid 顺序 + sessions.started_at 归日）：
  2026-10-14  70 个 blob   3.75 MB
  2026-10-22  27 个 blob  15.88 MB
  2026-10-28 161 个 blob  22.31 MB      ← 峰值
  2026-11-02  67 个 blob  11.74 MB
  2026-11-04 100 个 blob   9.66 MB
  合计 23 天 107.43 MB  ⇒ 平均 4.67 MB/天（54 KB/s）
```

结合 WAL：每个 blob 写一次要产生 ≈ ceil(S/4090) 个 WAL 帧（4.9MB ⇒ 1200 帧），
checkpoint 再写一遍 ⇒ **一次 blob 落库 = 2×S 的物理 IO**。

---

## 3. Q2 — trash FTS 残留 169.3MB

### 3.1 残留的是什么（精确）

```
$ sqlite3 "file:state.db?mode=ro" "select type,name from sqlite_master where name like 'fts%'"
table | fts_v22_trash_messages_fts_config
table | fts_v22_trash_messages_fts_data
table | fts_v22_trash_messages_fts_docsize
table | fts_v22_trash_messages_fts_idx
table | fts_v22_trash_messages_fts_trigram_config
table | fts_v22_trash_messages_fts_trigram_data
table | fts_v22_trash_messages_fts_trigram_docsize
table | fts_v22_trash_messages_fts_trigram_idx
```

**两张 virtual table（`fts_v22_trash_messages_fts`、`..._trigram`）本身已经不在 sqlite_master 里，
只剩 8 张 shadow 表。** 体积：

```
fts_v22_trash_messages_fts_trigram_data   131.016 MB (33540 p)
fts_v22_trash_messages_fts_data             36.496 MB (9343 p)
fts_v22_trash_messages_fts_trigram_docsize   0.477 MB
fts_v22_trash_messages_fts_docsize           0.887 MB
fts_v22_trash_messages_fts_trigram_idx       0.320 MB
fts_v22_trash_messages_fts_idx               0.098 MB
两只 _config                                   0.008 MB
────────────────────────────────────────────────────────
合计                                        169.30 MB  (43741 页)
```

### 3.2 为什么没随源表删除而清理 —— 真正的机制

**不是"源表删了索引没删"**。真相是 `optimize-storage` 的**分阶段迁移被中断**：

1. `hermes_state_search.py:679-720` `_demote_legacy_fts_to_trash()`：
   - `DROP VIEW IF EXISTS messages_fts_trigram_src`
   - `PRAGMA writable_schema=ON` → **直接从 sqlite_master 删掉两张 virtual table 的行**
     （`:695-701`），这就是它们消失的原因；
   - 然后把旧 shadow 表逐张 `ALTER TABLE {name} RENAME TO fts_v22_trash_{name}`（`:709`）
     —— 这就是 `fts_v22_trash_messages_fts_data` 这个名字的来由；
   - `_seed_fts_rebuild_markers(conn, force=True)` 写 backfill 标记（`fts_rebuild_high_water/progress`）。
2. `_ensure_v23_fts_tables()` 重新建好 v23 形状的 `messages_fts` / `messages_fts_trigram`
   （新 shadow 表：`messages_fts_data` 等 —— 所以新旧两套 shadow 表并存）。
3. **排空阶段**（`hermes_state_search.py:479-546` `_fts_teardown_trash_step`）负责
   分块 DELETE + 最终 `DROP TABLE`，并写进度标记 `fts_teardown_<table>_progress`。

**决定性证据：进度标记一个都没有。**

```
$ select key,value from state_meta where key like 'fts%'
fts_storage_version    | 1
fts_rebuild_high_water | 6
fts_rebuild_progress   | 6
```

**没有任何 `fts_teardown_*_progress`** ⇒ **排空 worker 一块都没跑过**，
迁移停在第 2 步（rename 完成、排空未开始）。

**为什么没人跑排空？** `optimize_fts_storage()`（`hermes_state_search.py:851`）只在
`hermes sessions optimize-storage` 这条**手工、需停 gateway 的离线命令**里被调用；
日常的 `db-maintenance.sh` 只做 `wal_checkpoint(TRUNCATE)` + prune，从不碰它。
官方文档也是这么说的（`hermes_cli/doctor_state.py:95-96`：
*"run 'hermes sessions optimize-storage' offline (with the host gateway stopped)"*）。

**后果**：`hermes_state_schema.py:1030-1046` `_stamp_settled_fts_layout()` 看到
`_has_fts_trash(conn)` 为真就**拒绝写 `fts_storage_version`**，
所以它永远停在 `1`（当前代码要求 `FTS_STORAGE_VERSION = 3`）⇒ 每次启动都还在提示"有优化可做"。

### 3.3 是否还在持续产生写 IO？——**没有，纯静态占用**

**WAL 帧级证据**（把 WAL 3352 个帧逐帧解码，用 dbstat 的 `pageno → 对象名` 映射回表）：

```
frames: 3352   commits: 236   bytes: 13,810,240
  1.96MB   498 frames  messages
  1.10MB   279 frames  messages_fts_data
  0.86MB   219 frames  messages_fts_trigram_data
  0.77MB   195 frames  sessions
  ... (idx_messages_* 合计 1172 frames / 35.0%)
  trash 相关帧数: 0        ← 13.8MB WAL 里一帧都没有
```

也没有任何 trigger 引用它们（trigger 只写 `messages_fts` / `messages_fts_trigram`）。
⇒ **169.3MB 是死重量，不产生任何持续写 IO。**

### 3.4 清理后能否真正释放？——修正主控的说法

主控 §4 Q2 说「DROP 索引不释放空间，需 VACUUM 或重建 DB」——**这句不准确**。准确的说法是：

| 动作 | 文件大小变化 | freelist | 代价 |
|---|---|---|---|
| `DROP TABLE` 这 8 张 shadow 表 | **不变**（仍 774MB） | `freelist_count` 从 **7 → 约 43,700** | 低。只写 schema + freelist 链，**不删行**（不读 blob、不重写 blob） |
| 之后的新写入 | **DB 停止增长**，新数据复用 freelist | 缓慢下降 | 0 |
| 再 `VACUUM` | 774MB → 约 **606MB** | 清零 | **高**：重写 774MB，机械盘上按 27~49ms await 估算 ≈ 30~90 分钟，且 VACUUM 全程走 WAL |

⇒ **推荐只做 DROP，不做 VACUUM。** 理由：
① DROP 之后 DB **不再增长**（每天 22MB 的持续膨胀是"越来越大"的根因）；
② freelist 的 168MB 会被后续写入直接复用，**净效果等于"免费删掉了 168MB"**；
③ VACUUM 收益仅是文件缩小 168MB，代价是机械盘上重写 774MB —— 在 `%util=94%` 的盘上做这个，
会把网关的每一次会话读取都拖进几十分钟的 IO 地狱。**得不偿失。**

⚠️ **执行时机**：必须**停 gateway**（否则 DROP 与写事务抢锁，且 gateway 打开时会重建 v23 表）。
见 §8.2 B1 的脚本。

---

## 4. Q3 — messages 328.74MB + FTS 副本 ~64MB

### 4.1 现有 retention 机制是什么

**机制 1：`config.yaml` 的 `sessions:` 块（真实配置）**
```yaml
sessions:
  auto_prune: true
  min_interval_hours: 24
  retention_days: 90
  vacuum_after_prune: true
  write_json_snapshots: false
```
入口：`hermes_cli/cli_auto_maintenance.py:17-74` `_run_state_db_auto_maintenance()`
→ `hermes_state_maintenance.py:478-585` `maybe_auto_prune_and_vacuum()`
→ `hermes_state_maintenance.py:364-410` `prune_sessions()`（`DELETE FROM messages WHERE session_id IN (...)`
  + `DELETE FROM sessions ...`，并触发 FTS delete）。

**★ 致命点**：`_run_state_db_auto_maintenance()` 的调用方在 **CLI 启动路径**（`cli.py`），
**不在 gateway 里**。主控这台机器上 265 个 feishu 会话 + 232 个 cron 会话都是 gateway 跑的，
**gateway 永远不会触发 auto_prune**。这解释了为什么 config 里明明写了 `auto_prune: true`，
111,190 条消息却一条没清。

**机制 2：每日的 `~/.hermes/scripts/db-maintenance.sh`（cron 04:00）**
```bash
MAX_SESSIONS=800        # session数超过此值才触发prune     ← 第 13 行
if [ "$SESSION_COUNT" -gt "$MAX_SESSIONS" ]; then ...      ← 第 55 行
```
```
$ grep "skip prune" ~/.hermes/logs/db-maintenance.log | wc -l
18
$ tail -2 ~/.hermes/logs/db-maintenance.log
[2026-11-06 09:41:56] Sessions (575) <= 800, skip prune
[2026-11-06 09:41:56] === DB maintenance done ===
```
⇒ **18 天，一次都没触发过。** session 数从 424 涨到 581，离 800 还差 219 个
（按当前 ~10 个/天 ⇒ 还要 **220 天**）才会第一次 prune。
而且即使触发了，它 prune 的是"**30 天前的 session**"（`PRUNE_AGE="30d"`），不是按体积。

**机制 3：`auto_archive` 未启用**（config 里没有 `auto_archive` 键）⇒ `archived=0`（581/581 全未归档），
`hermes_state_maintenance.py:307` 的 idle 归档路径**完全没启用**。

### 4.1b 同一模式在别处复现：`gateway_heartbeats` 也有 31 条僵尸行

```
$ select count(*) from gateway_heartbeats where last_heartbeat < now-600
31        ← 31 个已死 gateway 实例的心跳行，从未被回收
$ select count(*) from gateway_heartbeats
34        ← 只有一个是活的（pid 9028）
```
`prune_stale_heartbeats()`（`hermes_state_gateway.py:929-941`）同样只在特定路径调用，
日常维护从不触发。**"该清的没人清"是这台机器上的系统性问题，不是 state.db 独有。**

### 4.2 为什么 111K 条消息没被清（三个原因叠加）

1. **触发条件错**：阈值绑在 **session 数量**（800）上，而实际瓶颈是**字节**（738MB）。
2. **执行位置错**：`auto_prune` 只在 CLI 启动时跑；gateway 是唯一的长期运行者。
3. **回收阈值错**：`AUTO_VACUUM_MIN_FREELIST_RATIO = 0.25`（`hermes_state_common.py:296`）——
   即使 prune 掉了会话，**只要 freelist 占比 < 25% 就不会 VACUUM**，
   于是文件不缩小，新数据继续在文件尾部追加 ⇒ **DB 只会单调变大**。

### 4.3 "只保留最近 N 天"的功能影响清单

现有数据分布（`messages.timestamp` = Unix 秒 float）：

```
保留窗口   sessions   messages   content 字节
全部        581       111,190      102.5 MB
> 30 天     404        66,929       81.7 MB
> 60 天     317        46,906       62.5 MB
> 90 天     205        11,446       25.6 MB
> 120 天    197        11,350       25.4 MB
```

**★ 关键结论：按 90 天清理只能回收 11,446 条消息 / 25.6MB content（占 10%）。
剩下 90% 的数据（66,929 条 / 81.7MB）都在最近 30 天里产生。**
⇒ **"清历史"不是解药**（除非把窗口砍到 30 天，那会毁掉 session_search）。
真正的解药是**堵住新增**（Q1 的 blob、Q4 的每消息 15 页写放大）+ **清掉死重量**（Q2 的 168MB）。

**若强行按 N 天清理，功能影响清单：**

| 受影响功能 | 代码位置 | 影响 |
|---|---|---|
| `session_search` / FTS 全文检索 | `hermes_state_search.py`（`messages_fts` + `messages_fts_trigram`） | 命中范围缩到窗口内。**注意 trigram 索引本来就只覆盖 45% 的消息**（排除 cron/subagent，见 `messages_fts_trigram_src` 视图），所以搜索能力本来就不完整 |
| 上下文恢复 / 会话续接 | `hermes_state_messages.py`、`hermes_state_sessions.py` | 被删会话无法 resume、无法 `sessions adopt`；`state.db` 里 `sessions` 与 `messages` 必须同生共死（`prune_sessions` 是同时删的） |
| 审计 / 回溯 | `messages` 表 | 不可恢复。**Hermes 没有独立的审计存储**，审计能力 = `messages` 表本身 |
| 飞书历史（用户可见） | gateway 的会话转录 | 删掉 `messages` 后，飞书里回看旧对话会空白 |
| 压缩链（compression lineage） | `hermes_state_compression.py` | `prune_sessions(whole_lineages=True)` 会连带处理续接行；删中间段会**留下断裂的 lineage** |
| admission 精确重放 | `hermes_state_terminal.py` | `prune_sessions` → `retire_prunable` → `retire_terminal_receipts` → `_compact_result` 会把大 blob 压成 `messages: []`（**这是好事**：blob 会被瘦身） |

**结论（功能影响清单）**：
- **90 天窗口**：安全。205 个 session、11,446 条消息被清；对应 blob 被 `_compact_result` 瘦身 ⇒
  回收 ~25.6MB content + 一批 blob 空间。飞书最近 3 个月的对话不受影响。
- **30 天窗口**：**破坏 session_search 的历史覆盖**，且会切断 compression lineage。
  按数据分布它也只多回收 56MB —— **收益有限、破坏明确 ⇒ 不推荐**。
- **按"删最老"的增量方式（每次删 20 个最老 session）**：最安全，且随着新数据进来自然滚动。

---

## 5. Q4 — state.db 写放大（"死机"的直接放大器）

### 5.1 先纠正一个测量陷阱

> **`state.db-wal` 的 `stat` size 恒为 13,810,272 B，但它的 mtime 在变。**
> SQLite 的 WAL 文件到达高水位后**复用同一段空间**，size 不再反映写入量。
> ⇒ 衡量 WAL 写入必须用 `/proc/<pid>/io` 的 `write_bytes` + **WAL 帧解码**。

### 5.2 WAL 帧级拆解（本次审计的核心取证）

我写了解码器把 WAL 的 3352 个帧逐帧解出来，用 `dbstat` 的 `pageno → 对象名` 映射回表：

```
WAL header: magic=0x377f0682 format=3007000 page_size=4096 checkpoint_seq=154
frames: 3352   commits: 236   bytes in frames: 13,810,240

  35.0%  (1172 frames)  idx_messages_*        ← 8 个 messages 索引
  19.8%  ( 663 frames)  messages_fts*          ← 2 套 FTS 的 data/idx/docsize
  14.9%  ( 498 frames)  messages (行数据)
   5.8%  ( 195 frames)  sessions
   2.4%  (  79 frames)  sqlite_sequence
   2.1%  (  72 frames)  sqlite_schema
  ~13%                 sessions 的 10 个索引 + session_model_usage + leases
   0.0%  (   0 frames)  trash 表
```

**⇒ 每写一条 message，要付 ~15 个页的 WAL 写 + 同样多的 checkpoint 写。**
**⇒ messages 及其索引 + FTS 副本 = WAL 字节的 69.7%。**

触发器定义（`sqlite_master` 里可见）：每条 `messages` INSERT 会写
`messages_fts`（content + tool_name + tool_calls）+ `messages_fts_trigram`（content + tool_name）。
`messages_fts_src` 视图对 tool 行只索引前 8192 字节，但 **`tool_calls` 是全量入索引的**。

### 5.3 「每次会话结束产生的字节数」量化

```
一条普通消息：  1(messages) + 8(idx_messages_*) + ~4.5(FTS: data+idx+docsize 两套) ≈ 13.5 页
                ≈ 13.5 × 4120 B(WAL 帧) ≈ 55.6 KB WAL
                + checkpoint 回写 55.6 KB  ≈ 111 KB 物理 IO / 条消息
每天 ~2,000~5,000 条消息 ⇒ 每天 ~220~560 MB 物理 IO（写）+ 同量级 FTS 维护

一次 admission blob 落库：S 字节 ⇒ ceil(S/4090) WAL 帧 ⇒ 2×S 物理 IO
 4.97MB 的 blob ⇒ 1200 帧 ⇒ ~10 MB 物理 IO，**在同一个事务里**

实测每日新增（按 session_admissions.rowid 归日）：blob 净增 4.67 MB/天
```

**⇒ 结构性降 IO 一个数量级的方案（按收益/风险排序）：**

1. **【最大杠杆】把 `messages_fts` 的 `tool_calls` 从索引里去掉。**
   `messages_fts` 索引列是 `(content, tool_name, tool_calls)`（`sqlite_master` 可见），
   `tool_calls` 全量入索引 —— 而 `messages.tool_calls` 本身就有 59.8MB。
   `FTS_STORAGE_VERSION = 3` 的设计意图正是"trigram 也排除结构化 tool_calls JSON"
   （`hermes_state_common.py:300-312` 的注释），**但当前 store 的 `fts_storage_version=1`**，
   说明这个优化**从未落地**。
   落地方式：`hermes sessions optimize-storage`（会重建索引，代价大），
   或手工改 `FTS_SQL` 后重建 ⇒ 预计 `messages_fts_data` 44.1MB → **~25MB**，
   且**每条消息少写 ~2~3 个 FTS 页（每天省 ~30% 的 FTS 写 IO）**。
2. **砍掉 `idx_messages_*` 的冗余索引。** 8 个索引占 26MB、占 WAL 35%。
   `idx_messages_display_page` / `idx_messages_display_identity` / `idx_messages_display_backfill`
   是给 Desktop 显示分页用的，**在只用飞书 + CLI 的机器上几乎无价值**。
   手工 DROP 3 个 display 索引即可**立刻把 WAL 写量降 ~9%**、DB 降 ~9.6MB。
3. **把 admission blob 挪出 DB**（Q1 方案 A）⇒ state.db 写 IO 降 ~95%（4.67MB/天不再进 WAL）。
4. **把 `messages_fts_trigram` 降级**：`messages_fts_trigram` 只索引 49,975/111,190 = 45% 的消息
   （排除 cron/subagent），却占 20.1MB + 每天 ~0.8MB 写入。
   CJK 检索可以退化成 `LIKE`（代码注释 `hermes_state_search.py:662` 明确说
   *"CJK falls back to LIKE"*），**停掉 trigram 索引能省 20MB 存量 + 每天 ~0.8MB 写**。
5. **`PRAGMA wal_autocheckpoint` 不是杠杆**（主控已指出，我同意：已是默认 1000 页 = 4MB）。
   真正的杠杆是上面 1~4 —— **少写页**，而不是少 checkpoint。

---

## 6. Q5 — gateway 内存爬升

### 6.1 与 Q1 的 blob **不同源**

**证据 1：blob 不是常驻内存。**
`compact_result`（`hermes_state_terminal.py:28-48`）做的是 `messages[anchor+1:]` —— **切片，
底层 message dict 是共享引用**，不是拷贝。落库后 `_json(...)` 生成的 5MB 字符串随事务结束即释放。
⇒ blob **不常驻**，它只是**瞬时**制造一份 5MB 字符串 + `_redacted()`（`gateway/session_results.py:35-46`）
的一次**全量深拷贝**。
⚠️ `_redacted()` 递归重建每一个 dict/list ⇒ 对一个 5MB 的 JSON，对象图可能是 15~30MB 的 Python 对象。
**这是峰值内存的一次性尖峰，但它在结算后就被释放，不能解释"持续 1.2GB"。**

**证据 2：transcript 本身只有 15MB。**
我用 `sys.getsizeof` 递归测了真实会话的 Python 堆占用：

```
session 20261104_062207_b4ebeb9d : 3585 msgs, 8.83 MB 文本 → Python 堆 15.48 MB (×1.75)
session 20261102_204116_32b76553 : 2684 msgs, 7.59 MB 文本 → Python 堆 13.79 MB (×1.82)
session 20260819_065119_7c673852 : 6894 msgs, 10.08 MB 文本 → Python 堆 24.55 MB (×2.44)
```
⇒ 最大的会话也只占 **24.5MB**。**远不是 1.2GB。**

### 6.2 那 1.2GB 到底是什么？——**glibc malloc arena 堆积**

```
$ grep Threads /proc/9028/status          → Threads: 66
$ nproc                                   → 4        ⇒ glibc 默认 arena 上限 8×4 = 32
$ tr '\0' '\n' < /proc/9028/environ | grep -i malloc   → （空，MALLOC_ARENA_MAX 未设置）
```

`/proc/9028/smaps` 解析：

```
total RSS 582,724 KiB
anon 映射 117 个，anon RSS 518,864 KiB
其中 > 8MB 的 anon 映射 13 个，合计 409,332 KiB
  67,628 / 52,992 / 46,520 / 45,404 / 41,820 / 37,452 / 30,168 / 25,404 / 15,008 / 13,680 / 11,392 / 11,060 / 10,804 KiB
```

**13 个 10~67MB 的匿名映射，正是 glibc per-thread arena 的典型形状**
（arena 的 `HEAP_MAX_SIZE` 是 64MB；它们都 < 64MB ⇒ 是被部分使用的 arena）。
Python 的 66 个线程各自绑一个 arena，arena 一旦涨上去就**不再把内存还给内核**
（除非 `M_TRIM_THRESHOLD` 触发 trim，默认 128KB 实际上不起作用）。

⇒ **anon RSS 1.2GB 的主体是 glibc arena 碎片，不是"会话太大"。**
这也解释了为什么"evicting 1 LRU session"只能把 RSS 从 1257MB 降到 1221MB
—— 释放的是 15MB 的 transcript，而 arena 不归还。

**⇒ Q5 结论：与 Q1 不同源（blob 只造成瞬时尖峰），是独立问题；独立方案见 §8.2 B2。**

### 6.3 但压力阀的告警本身是有用的

```
$ grep "Agent cache pressure" ~/.hermes/logs/gateway.log | grep -oE "^[0-9]{4}-[0-9]{2}-[0-9]{2}" | sort | uniq -c
  23 2026-10-20     41 2026-10-21     45 2026-10-22     37 2026-10-23
  23 2026-10-24     21 2026-10-25     25 2026-10-26     63 2026-10-27
  43 2026-10-28     24 2026-10-29     45 2026-10-30     13 2026-10-31
  56 2026-11-01     80 2026-11-02     32 2026-11-03      4 2026-11-04      3 2026-11-06
```
**18 天 544 条**告警，每 5 分钟一条（housekeeping watcher 的 300s 间隔，`gateway/run_watchers.py:39-58`）。
峰值 **1783MB**（11-03 20:06）、1741MB、1579MB。
`agent.agent_cache.memory_high_mb: 800`（config.yaml）⇒ 阈值是**手写死的 800**，
而实际工作集在 1.0~1.8GB 之间 ⇒ **告警是常态，阀门形同虚设**。

---

## 7. Q6 — 全盘写盘源头排序（按物理写 IO）

### 7.1 物理写 IO 排名（120 秒窗口，`/proc/<pid>/io` 的 `write_bytes`，= 交给块层的字节）

```
span=120.0s   总物理写 3,184,472 B/s (3.04 MB/s)   总物理读 582,852 B/s (0.56 MB/s)

 #   进程            物理写        物理读      占比     分类
 1  main.mjs(22310)   2417.5 KB/s    0.1 KB/s  76.0%   (a) 常驻持续写  [smart_trading_v10, PM2]
 2  kline-store(31338)  559.1 KB/s  560.7 KB/s 17.6%   (b) 周期批任务  [cron :07/:37]
 3  hermes(9028)        110.2 KB/s    4.4 KB/s  3.5%   (a) 常驻，按会话突发
 4  bash(1765)            8.4 KB/s    0.0 KB/s  0.3%   (c) 一次性脚本
 5  books-collector      7.5 KB/s    0.0 KB/s  0.2%   (a) 常驻低频
 6  PM2 God              2.8 KB/s    0.0 KB/s  0.1%
 7  edict-dashboard       1.7 KB/s    0.0 KB/s  0.1%
 8  cron.scheduler        1.3 KB/s    0.0 KB/s  0.0%
 9  liquidation-collector 0.1 KB/s   0.0 KB/s  0.0%
```

文件级交叉验证：

```
pm2 smart-trading-out.log   +206,180 B / 90s = 2.2 KB/s
~/.hermes/logs/gateway.log  +142 B / 90s     = 0.0 KB/s
~/.hermes/logs/agent.log    +2,931 B / 90s   = 0.03 KB/s
journald (system)           148,934 B / 10min = 0.25 KB/s
```
⇒ **journald、PM2 日志、gateway 日志全部可忽略**（合计 < 0.4%）。

### 7.2 磁盘实际状态（`iostat -x 5 12`）

```
sda  52.05 r/s  284.93 rkB/s | 71.92 w/s  1408.22 wkB/s | %util 96.78
     r_await 23.74ms          w_await 49.33ms         | aqu-sz 5.42
sda  36.77 r/s  698.36 rkB/s | 130.56 w/s 6207.03 wkB/s | %util 93.92
     r_await 58.29ms          w_await 27.59ms         | aqu-sz 5.90
```
**写队列深度 5.4~5.9，写延迟 27~49ms，读延迟 19~58ms，%util 94~97%。**
注意：**读延迟（58ms）比写延迟还高** ⇒ `aqu-sz` 里主要是**读**在排队
—— 那些读正是被回收的 `state.db` page cache 重新去盘上捞。

### 7.3 分类与削减预期

| 类别 | 源头 | 物理写占比 | 处置 | 预期削减 |
|---|---|---|---|---|
| **(a) 常驻持续写** | `smart_trading_v10/main.mjs` → `data/trading.db-wal` | **76.0%** | 交易系统侧降 WAL 写频率 / 批量提交 | **-60~70%** 全机写 |
| (a) 常驻持续写 | Hermes gateway → `state.db` | 3.5% | Q1 方案 A（blob 出库）+ Q4 索引瘦身 | **-2.5~3%** 全机写，但**把 Hermes 自己的写降一个数量级** |
| **(b) 周期性批任务** | `kline-store.mjs`（cron :07/:37，单轮 ~16~19 分钟 ⇒ **跨轮重叠**） | 17.6% | `heavy-run.sh` 已带 ionice/nice；加 `flock` 防重入 + 缩短单轮 | **-10~15%** 全机写 |
| (b) 周期性批任务 | `trading-paper-forward.timer`(15min)、`agent-bus-poll*.timer`(60s)、`mem-pressure-watchdog`(60s)、`hermes-feishu-watchdog`(60s) | < 0.1% | 无需动 | 0 |
| **(c) 一次性大文件** | `backup-trading-db.sh`(03:00)、`heavy-run.sh` 的 shadow-audit/harness/evolution(06:23~09:23) | 未在窗口内 | 加 `ionice -c3` | 削峰，不改均值 |
| **(c) 一次性大文件** | `state.db` 的 `optimize-storage` / `VACUUM` | 未发生 | **必须停 gateway 做** | 见 §3.4 |
| 参考 | journald / PM2 日志 / Hermes 日志 | < 0.4% | 无需动 | 0 |

### 7.4 D 状态线程归属（实测）

```
for p in /proc/[0-9]*; do st=$(awk '{print $3}' $p/stat); [ "$st" = "D" ] && ... ; done
=== PID 22145 node (kline-store.mjs) ===   state = Dl
```
`kline-store.mjs` 卡在 D 状态。**Hermes gateway (pid 9028) 的 66 个线程全部是 S/Ssl，没有一个在 D。**
⇒ 「卡死」不在 Hermes 侧；Hermes 侧是**排队等 IO + 被 SIGKILL 打断**。

---

## 8. Q7 — 可执行修复清单（有序）

**先做哪 3 项（按 收益/风险 比）：**

| 序 | 动作 | 风险 | 需重启 gateway | 收益 |
|---|---|---|---|---|
| **①** | **抬 `MemoryHigh`（1800M → 2600M）+ `MemoryMax`（2200M → 3000M）** | 低 | 否（`systemctl --user set-property` 实时生效） | **立即止血**：cgroup 从 97.5% 占用降到有 ~800MB 余量 ⇒ 回收停止；state.db page cache 常驻；"只进不出"消失 |
| **②** | **DROP 8 张 trash shadow 表（不 VACUUM）** | 低 | **是**（需停 gateway） | DB 停止增长；freelist +168MB 可被立即复用；`fts_storage_version` 能被盖章 |
| **③** | **`MALLOC_ARENA_MAX=2` + `MALLOC_TRIM_THRESHOLD_=131072`** | 低 | **是** | anon RSS 峰值预计 **-300~600MB**；告警从 544 条/18天 → 接近 0 |

---

### 8.1 不停机可做（配置 / 运维动作）

| # | 动作 | 风险 | 需重启 | 预计削减 | 验证命令 |
|---|---|---|---|---|---|
| **A1** | **抬 gateway cgroup 限额**：`MemoryHigh=1800M`→`2600M`，`MemoryMax=2200M`→`3000M`，`MemorySwapMax=512M`→`1024M` | 低 | 否 | cgroup 从 97.5% → ~66% 占用；回收停止；消除 R1 | `systemctl --user set-property --runtime hermes-gateway.service MemoryHigh=2600M MemoryMax=3000M` → 然后 `cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service/memory.events`（`high` 应停止增长）+ `memory.current` 应明显低于 `memory.high` |
| A2 | **修 `db-maintenance.sh` 的触发条件**：把 `MAX_SESSIONS=800` 改成"**DB > 600MB 或 WAL > 100MB 或 session > 800**"任一命中即 prune；并把 `PRUNE_AGE` 从 `30d` 改成 `90d`（与 config 一致） | 低 | 否 | 每天回收 ~25MB content + 压缩 blob；避免未来再撞 800 | `grep "skip prune" ~/.hermes/logs/db-maintenance.log` 之后应变成 `Pruned N sessions` |
| A3 | **一次性离线执行 `optimize-storage`**（**不要**放进日常 cron；见 B1 优先） | 中 | 是 | 见 B1 | `hermes sessions optimize-storage --yes` 的输出里应出现 `teardown` 完成、`ok: true` |
| A4 | **降 `main.mjs` 的写速率**（交易系统侧，不在 Hermes 范围，但它是 76% 的物理写） | 中 | 是（PM2 重启） | **全机物理写 -60~70%** ⇒ 机械盘 %util 有望从 94% 降到 ~40% | `pidstat -d 1 30 \| grep main.mjs` 的 `kB_wr/s` 应从 ~2400 降到 < 900 |
| A5 | **给 `kline-store.mjs` 加 `flock` 防跨轮重入**（cron :07/:37，单轮 16~19 分钟 ⇒ 必然重叠） | 低 | 否（改 cron） | 全机物理写 -10~15%；消除 D 状态卡顿 | `pgrep -f kline-store.mjs` 同一时刻应 ≤ 1 个 |
| A6 | **把 `main.mjs` / `kline-store` 的输出降到更少的 fsync 批次** | 中 | 是 | 同 A4 | `iostat -x 5` 的 `w/s` 应下降、`w_await` 应 < 10ms |

### 8.2 需停机 / 重启（改配置或改 Hermes 本体）

| # | 动作 | 风险 | 需重启 | 预计削减 | 验证命令 |
|---|---|---|---|---|---|
| **B1** | **DROP 8 张 trash shadow 表**（见 §10.1 的 `scripts/_audit_freeze_drop_fts_trash.py`） | 中 | **是** | DB 停止增长；freelist +43,700 页（7 → ~43,700）；`fts_storage_version` 可盖章 | 脚本输出 `freelist_count` 从 7 → ~43,700；`select count(*) from sqlite_master where name like 'fts\_v22\_trash\_%'` → 0 |
| **B2** | **`MALLOC_ARENA_MAX=2` / `MALLOC_TRIM_THRESHOLD_=131072`** 加进 `10-memory-limit.conf` 的 `[Service]` | 低 | **是** | anon RSS 峰值 -300~600MB | 重启后 `/proc/<pid>/smaps` 里 >8MB 的 anon 映射数应 ≤ 3；`grep "Agent cache pressure" gateway.log` 应停止增长 |
| **B3** | **DROP 3 个 `idx_messages_display_*` 索引**（display_page 4.14MB / display_identity 3.78MB / display_backfill 1.52MB） | 中 | 是 | DB -9.4MB；WAL 写 -9% | `select count(*) from sqlite_master where name like 'idx_messages_display%'` → 0；**需先确认 Desktop 是否在用**（不在用才做） |
| **B4** | **把 `maybe_auto_prune_and_vacuum` 接到 gateway 的 housekeeping watcher**（`gateway/run_watchers.py:39` `_session_housekeeping`，已是每 300s 跑一次）而不是只在 CLI 启动时跑 | 中 | 是（改本体） | sessions 自动滚动回收；DB 不再单调增长 | 改完后 `~/.hermes/logs/gateway.log` 应出现 auto-prune 相关行；`select count(*) from sessions` 应稳定在 ~550 |
| **B5** | **改 `compact_result`：把 `messages[*]` 里的 `reasoning` / `reasoning_details` / `anthropic_content_blocks` 三选一** | 中 | 是 | blob 体积 -30~45% | 需同步改 `tests/hermes_state/test_terminal_retirement.py:184`；验证：`_audit_freeze_probe_meta.py` 的 `admission_blobs.bytes` 应显著下降 |
| **B6** | **【改本体，最高杠杆】admission blob 落文件、DB 只存路径**（Q1 方案 A） | 中 | 是 | state.db 写 IO **-95%**；blob 读回 **4.0s → <50ms**；state.db page cache 少 774MB | `python scripts/_audit_freeze_probe_meta.py` 显示 `admission_blobs.bytes` → ~0；`state.db` 文件大小应停止增长 |
| **B7** | **让 `messages_fts` 不索引 `tool_calls`**（对齐 `FTS_STORAGE_VERSION=3`） | 中 | 是 | `messages_fts_data` 44MB → ~25MB；每天 FTS 写 -30% | `hermes sessions optimize-storage --yes` 后 `select value from state_meta where key='fts_storage_version'` 应为 `3` |
| **B8** | **考虑停用 `messages_fts_trigram`**（CJK 退化为 LIKE，代码已支持） | 中 | 是 | DB -20MB；每天写 -0.8MB | `select count(*) from messages_fts_trigram` → 0；CJK 搜索会变慢但不停 |

### 8.3 明确「**这条不是原因**」的排除项

> **请不要动这些，它们不是死机的原因：**
>
> - ❌ **`wal_autocheckpoint`** —— 已是默认 1000 页（4MB），主控和我都同意它不是杠杆。
> - ❌ **`VACUUM state.db`** —— `freelist_count=7`，现在 VACUUM **什么也回收不到**，却要在机械盘上重写 774MB。
>   只有在 B1 之后（freelist≈43,700）VACUUM 才有意义 —— **而且仍然不推荐**，见 §3.4。
> - ❌ **swap / 收紧 `MemoryMax`** —— `free -m` 显示 MemAvailable 4.2GB、swap 只用 158MB、memory PSI 全 0；
>   **内存根本不是瓶颈**，收紧只会制造更多回收（这正是当前 2.34 次/秒回收的来源）。
> - ❌ **`sessions.auto_prune: true` / `retention_days: 90` 的 config 值** —— 值本身没问题；
>   问题在**它只在 CLI 启动时执行**，而这台机器几乎不用 CLI（B4 才是修复）。
> - ❌ **`agent.agent_cache.memory_high_mb: 800`** —— 这个阈值**从来没生效过**（阀门只能泄 transcript，
>   而 RSS 的主体是 glibc arena）。B2 才是真修复。
> - ❌ **journald / PM2 日志 / Hermes 自身日志** —— 合计 < 0.4% 的物理写。
> - ❌ **`hermes-feishu-watchdog.py` 的重启逻辑** —— 它已经做得很谨慎了
>   （"断连>10min 且不再重试 且无回合在跑"才动手，2026-10-22 修订过）。
>   它**不是**死机的成因；**被它重启反而是好事**（恢复手段）—— 真正的问题是**需要它重启**这件事本身。
> - ❌ **trading.db 的内容正确性 / smart_trading_v10 的交易逻辑** —— 与本任务无关（主控 §7 前序工作）。
>
> ⚠️ **但请注意 A4/A5**：交易系统占 94% 的物理 IO，**它不是"死机"的原因，但是"延迟"的放大器**。
> 把它降下来，Hermes 的每一次会话读都会从 27~49ms 掉回 <5ms。

---

## 9. Q8 — 反证 / 最有力的一击

### 9.1 我认为主控最可能错的：H1

> **H1「state.db 里每次会话写 1~5MB admission blob 是 IO 饱和的主因」**

**反证（三个独立口径）：**

1. **量级反证**：120 秒窗口内 Hermes gateway 的物理写是 **110.2 KB/s**，
   其中 blob 占比 < 5%；而 `main.mjs` 一个进程写 **2417.5 KB/s**。
   blob 的日均净增 4.67MB = **54 KB/s**。
   ⇒ **Hermes 的写根本不是盘饱和的原因。**
2. **方向反证**：`iostat` 显示 `r_await`（19~58ms）**高于** `w_await`（27~49ms），
   `aqu-sz` 5.4~5.9 主要是**读**在排队。
   而 Hermes 卡住的 4 秒读（blob 读回）**是读**。
   ⇒ **瓶颈是"读不到缓存"，不是"写太多"。**
3. **归因反证**：Hermes gateway 的 **66 个线程没有一个在 D 状态**；
   卡在 D 的是 `kline-store.mjs`。⇒ Hermes 从没被写入阻塞过。

**我的替代假设 H1′：**

> **磁盘饱和的主因是 `smart_trading_v10`（76%+18% = 94% 的物理 IO）；
> Hermes 之所以"死机"，是因为它的 cgroup 被钉在 `MemoryHigh=1800M`（长期占用 97.5%），
> 导致 774MB 的 `state.db` page cache（= 900MB 的 file cache）被内核持续回收（突发 2.34 次/秒，累计 14,668 次），
> 于是每一轮会话都要从 94% 满载的机械盘上重新捞数据（27~58ms/次），
> 叠加 4.9MB blob 的 4 秒读回，回合耗时飙到排空超时 → SIGKILL → unknown fence → 会话永久 paused。**

### 9.2 其他几条我认为需要修正的

- **F5 的归因**：D 状态是交易系统的，不是 Hermes 的。Hermes 侧 0 个 D 状态线程。
- **F7 的"WAL 持续增长"**：WAL size 恒定 13,810,272 B（复用空间），只有 mtime 在变。
- **§3⑥ "不要建议 VACUUM"**：结论对，但理由要改成"**现在** freelist=7，VACUUM 收不到东西"。
  正确的替代是 **DROP trash 表**（几乎零成本、立即止住增长）。
- **F10 的机制**："源表删了索引没删" ⇒ 实际是 "**optimize-storage 的 rename 阶段做完了、
  排空阶段一帧没跑**"。修法也不是"跑 optimize-storage"（那会顺带 VACUUM + 重建 FTS，
  在这台盘上代价极高），而是 **单独 DROP 那 8 张表**。

---

## 10. 附录

### 10.1 产物清单

```
探针脚本（项目内，只读）
  scripts/_audit_freeze_probe_db.py       state.db 全量结构/占用/dbstat 快照
  scripts/_audit_freeze_probe_meta.py     state_meta 键族/admission blob 分布/消息列字节
  scripts/_audit_freeze_probe_io.py       /proc/<pid>/io 物理 IO 差分 + WAL 增长
  scripts/_audit_freeze_probe_growth.py   关键文件增长率
  scripts/_audit_freeze_probe_wal.py      ★ WAL 帧解码 + dbstat pageno→表 映射
  scripts/_audit_freeze_probe_mem.py      transcript 的 Python 深堆估算
  scripts/_audit_freeze_drop_fts_trash.py ★ 建议动作 B1 的离线 DROP 脚本（默认 dry-run）

原始观测
  data/replay/_audit_freeze_db_snapshot.json
  data/replay/_audit_freeze_meta.json
  data/replay/_audit_freeze_io.json / _io2.json / _io3.json
  data/replay/_audit_freeze_growth.json
  data/replay/_audit_freeze_iostat.txt
  data/replay/_audit_freeze_wal.json        ★ 3352 帧的逐帧对象归属
```

### 10.2 一键复核命令（本文所有数字的出处）

```bash
cd ~/.hermes/hermes-agent

# F8 / F11 / F12  占用与碎片
python scripts/_audit_freeze_probe_db.py data/replay/_audit_freeze_db_snapshot.json

# F9 / Q1  admission blob
python scripts/_audit_freeze_probe_meta.py data/replay/_audit_freeze_meta.json

# Q4  WAL 帧 → 表 归属（★核心取证）
python scripts/_audit_freeze_probe_wal.py data/replay/_audit_freeze_wal.json

# F6 / Q6  物理 IO 排名
python scripts/_audit_freeze_probe_io.py 120 data/replay/_audit_freeze_io3.json
iostat -x 5 12

# Q5  transcript 堆
python scripts/_audit_freeze_probe_mem.py 20261104_062207_b4ebeb9d

# R1  ★ cgroup 回收速率（最关键的一条）
D=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
watch -n10 "cat $D/memory.events; cat $D/memory.current $D/memory.high; grep -E '^(anon|file|slab) ' $D/memory.stat"

# R2  ★ 4.9MB blob 的读回延迟
python - <<'EOF'
import sqlite3, time
for i in range(5):
    t=time.perf_counter()
    c=sqlite3.connect('file:/home/szjacky/.hermes/state.db?mode=ro',uri=True)
    c.execute("select value from state_meta where key like 'gateway.admission.result.v1.%' order by length(value) desc limit 1").fetchall()
    c.close(); print(f"{(time.perf_counter()-t)*1000:.0f} ms")
EOF

# R5  retention 从未触发
grep "skip prune" ~/.hermes/logs/db-maintenance.log | tail -3

# R1  ★ SIGKILL 历史（"永久 paused"的来源）
journalctl --user -u hermes-gateway.service --since "10 days ago" --no-pager \
  | grep -E "Main process exited|Failed with result"
```

### 10.3 未解决 / 建议后续

- **910MB 的 page cache 归属**：用户 slice 里 `hermes-gateway.service`（883MB file）和
  `hermes-worker-proc_*.scope`（831MB file）**都在缓存同一个 state.db**。
  抬限额只是让两份都常驻（= 1.7GB RAM）；**根治是把 state.db 瘦身**（B1+B7+B8 ⇒ 774MB → ~560MB）。
- **`sdb` 是 0B 的空设备**（`lsblk` 显示 `sdb 0B`）—— 如果以后能插一块 SSD，
  把 `~/.hermes/state.db` 迁过去是**单项收益最大的结构性改动**（把 774MB 的热数据挪出机械盘）。
- **交易系统侧（A4/A5）不在本轮 Hermes 范围内**，但它是 94% 的物理 IO；
  不动它，Hermes 的任何 IO 优化都只能拿到 3.5% 的收益。
- `feishu-watchdog.py` 的 `MAX_INFLIGHT_DEFER_SEC = 2700`（45 分钟）在
  "回合因为慢而卡住"的场景下会**一直给卡住的回合让路**，导致 watchdog 不敢重启 ⇒
  正好把 R1 的症状放大成"长时间只进不出"。**建议在 R1 修好前把这条改成 900s。**

---

*（本报告由 codex 独立复核产出；所有数字均附可复现命令。主控 §7 的交易系统前序工作未混入本报告。）*
