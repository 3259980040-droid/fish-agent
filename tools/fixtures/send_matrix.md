# 群消息发送状态矩阵

谁能把一条回复送进群、什么时候可以、结果未知时能不能重试——用一张表回答，不再靠逐条
review 发送路径。表的可执行版本是 `tools/test_send_matrix.py`：每一行是一条数据，
失败信息带格子坐标（例如 `engine=fish,listen=True,reply=False,reservation=active`）。

判据只取既有的公开谓词，没有新造语义：

| 问的是谁 | 谓词 | 生产代码 |
|---|---|---|
| 新 Fish 真发 | `GroupPolicy.observe / reserve / live` | `harness/group_policy.py` |
| 旧 DSH 发送 | `GroupDialogue._engine_blocks / enabled` | `harness/group_dialogue.py` |
| 旧 DSH 逐段复判 | `GroupDialogue.valid(job)` | `harness/group_dialogue.py:201` |
| 单发送者最后一道 | `QQAgentLoop._fish_live` | `harness/qq_agent_loop.py:399` |

## 1. 权限矩阵（engine × listen × reply）

`read` 不在表里，因为它不参与任何一个发送谓词（见第 4 节）。
`observe` 只看 listen（`group_policy.py:101`）；`reserve` 要求 listen 且 reply
（`group_policy.py:166`）；`live` 要求 engine=fish 且 listen 且 reply
（`group_policy.py:228`）；`enabled` 要求 engine 属于 {dsh, shadow} 且 listen 且 reply
（`group_dialogue.py:15,68`）。

| engine | listen | reply | observe 收下 | reserve 给 token | live（新 Fish 可发） | enabled（旧 DSH 可发） |
|---|---|---|---|---|---|---|
| dsh    | True  | True  | 是 | **是** | 否 | **是** |
| dsh    | True  | False | 是 | 否 | 否 | 否 |
| dsh    | False | True  | 否 | 否 | 否 | 否 |
| dsh    | False | False | 否 | 否 | 否 | 否 |
| shadow | True  | True  | 是 | **是** | 否 | **是** |
| shadow | True  | False | 是 | 否 | 否 | 否 |
| shadow | False | True  | 否 | 否 | 否 | 否 |
| shadow | False | False | 否 | 否 | 否 | 否 |
| fish   | True  | True  | 是 | **是** | **是** | 否 |
| fish   | True  | False | 是 | 否 | 否 | 否 |
| fish   | False | True  | 否 | 否 | 否 | 否 |
| fish   | False | False | 否 | 否 | 否 | 否 |

两个必须记住的读法：

1. **reserve 不是许可。** `_candidate` 从头到尾不看 engine，所以 dsh / shadow 下也照样拿到
   token；`live` 才是读 engine 的那一处。把 token 当"可以发"是这条链上最容易犯的错。
2. **不变量：一格最多一个发送主体。** listen 且 reply 的格子里 `live` 与 `enabled` 恰好一真一假；
   其余格子里两者皆假。断言的是"数量必须是 0 或 1"，不是"两个都不能发就算过"。

`listen=False, reply=True` 那三行**只能由原始写入到达**：`configure` 在 listen 关闭时强制
把 reply 一起关掉（`group_policy.py:87`）。它们留在矩阵里，是因为"绝不会出现
reply=True 而 listen=False 的行"这个假设必须由谓词本身保证，不能只靠写入路径保证。

### shadow 的语义

engine=shadow 是"只算不发"：新 Fish 会在群里算出一份候选并记账，不发送；旧 DSH 照旧真发。
所以 `live` 必须为假、`enabled` 必须为真，两者都单独断言
（`test_shadow_keeps_the_old_sender_and_never_the_new_one`）。shadow 不是"新引擎的
调试档位"，它就是"旧引擎仍然说话"的配置。

## 2. 预约矩阵（一条消息的预约史）

| reservation | 自己持 token | live | 同一游标再 reserve | 含义 | 负责的代码 |
|---|---|---|---|---|---|
| 无 | 否 | 否 | 第一次就能拿到 | 还没人预约，`reserve` 会给 | `group_policy.py:190` |
| mine（本消息） | 是 | engine=fish 时为真 | 空（同一游标只授予一次） | 唯一有效的发送者 | `reserve:195`、`live:228` |
| other（别的消息） | 否 | 否 | 空（该群已有 live 预约） | 一条群消息只允许一个发送主体 | `_candidate:169` |
| disabled（被切换退休） | 是，但已 disabled | 否 | 空（handled 已前进） | 开关或引擎一变，已拿到的许可立刻作废 | `_retire:150`、`configure:93` |
| sent | 是，但已 sent | 否 | 空 | 已经发出，游标关闭 | `finish:230` |
| uncertain | 是，但已 uncertain | 否 | 空 | 结果未知，永不重试 | `finish:235`、`qq_agent_loop.py:541` |

补充两条同格不变量：

* `other` 格子里还要断言"新消息不能取消已经拿到 token 的那条"——`live` 故意不复问
  candidate（见 `group_policy.py:207-213` 的注释），否则一条比回复晚到的消息会让每一句
  回复都发不出去。
* `uncertain` 的"不再授予"由**两层**保证：`handled` 前进（`finish:237`），以及
  `reservations` 表上的 `UNIQUE(chat,cursor)`（`group_policy.py:44`）。测试把 handled
  手动归零后再试一次，仍必须拿不到 token —— 两层里任何一层都不能是唯一防线。

## 3. 撤销矩阵（token 已经在手，开关变了）

三种撤销 × 两种落地方式：`configure` 是主人自己的路径，会顺手退休 token；原始写入不退
休，只留 `live` 的复判兜住。

| 撤销 | live | enabled | token 状态 | 同游标再 reserve |
|---|---|---|---|---|
| engine→dsh | 立刻假 | **转为真**（旧 DSH 拿回发送权） | configure：disabled｜原始写入：仍 active | 空 |
| reply→False | 立刻假 | 假 | 同上 | 空 |
| listen→False | 立刻假 | 假 | 同上 | 空 |

engine→dsh 那一行要特别读：这不是"两个引擎都不能发"，而是**发送权转移**。
`_engine_blocks` 与 `live` 是互补的两半，不是两道串联的闸门。

原始写入那一列的意义：`live` 自己会把 engine/listen/reply 重新读一遍，所以即使有别的
写入路径没有退休旧 token，也发不出去（`group_policy.py:222-228`）。

## 4. read 不是发送闸门

`read` 只决定"能不能读历史"：`group_read.read_group` 在 `group_read.py:473` 检查它，
`authorized_groups` 用它列出可读的群。它不出现在 `observe`、`_candidate`、
`reserve`、`live`、`_engine_blocks`、`enabled`、`valid` 的任何一条判断里。

矩阵对 3×2×2 个 (engine, listen, reply) 组合各跑 read=True / False 两遍，断言
（observe 收下、是否拿到 token、live、enabled）四元组完全相同。read 的正向效果
（该读到什么、越权怎么拒）属于 `test_group_read` 的范围，这里不重复。

## 5. 在途任务矩阵

| 在途 DSH 任务 | 新 Fish 可发 | 旧 DSH `valid(job)` |
|---|---|---|
| 无（任务已 sent） | engine=fish 时为真 | 假 |
| 有（state=generating） | engine=fish 时为真（不受影响） | engine 属于 {dsh,shadow} 时为真 |

读法：**在途任务不自带权限。** `GroupDialogue.valid` 每次都把 `enabled()` 重新 AND 一遍
（`group_dialogue.py:201-207`），所以引擎一换，正在生成的旧回复在下一个分段就停。反方向
也不成立：在途任务不会占住新 Fish 的预约，两个引擎之间没有互斥死锁。

真正把在途任务停掉的是派发处：`qq_agent_loop._on_group` 在 engine=fish 时先
`_cancel_group_dialogues(chat)` 再启动 fish（`qq_agent_loop.py:322-325,366-384`）。那一层由
`test_engine_switch_dsh` 与 `test_group_fish_send` 覆盖，本矩阵只钉住谓词层的正交性。

## 6. 本矩阵之外、被固定为真的层

这些层回答的是别的问题，矩阵里一律按"已允许"处理，各自有既有测试：

| 层 | 谓词 | 谁覆盖 |
|---|---|---|
| 群是否部署、是否在允许名单、主人是否暂停 | `GroupWorkflow.allowed` | `test_group_engine`、`test_group_fish_send` |
| 主人总暂停 | `QQAgentLoop._fish_live` 里的 `paused` | 矩阵里单独一行断言它是否决项 |
| A/B 测试窗口自己的 listen/reply | `GroupWorkflow.policy`（`GroupTests` 的 store） | `test_engine_switch_dsh` |
| 每日预算、价值判断、合并窗口、冷却、静默时段 | `_candidate`、`group_decide` | `test_worth_speaking`、`test_group_shadow` |

矩阵把 `allowed` 固定为真的理由：它问的是"这个群现在归谁管"，不是"哪个引擎能发"。它在
矩阵里只出现一次（`test_the_approval_layer_is_a_separate_permission`），断言两件事：
allowed=False 时旧 DSH 一律不发；新 Fish 的 live 不受它影响，由自己的复判和暂停决定。

## 7. 一个已知的、故意的非对称

`GroupPolicy.valid()` **不看 engine**，它只复问游标（`group_policy.py:199-205`）。所以引擎
回滚之后它仍然为真。这不是漏洞，而是分工：它是旧 DSH 侧的游标校验，engine 由
`GroupDialogue.enabled()` 负责，两者在 `GroupDialogue.valid(job)` 里 AND 起来。
`test_policy_valid_is_not_a_send_permission` 把这三个值放在同一格里一起断言：

* `policy.valid(token)` 真、`policy.live(token)` 假 —— 引擎回滚后是真实现象；
* 同一个 token 在 `dialogue.valid(job)` 里是假 —— 组合起来才安全。

**结论：任何新代码都不得把 `GroupPolicy.valid()` 当成"可以发"。**

## 8. engine=dsh 冻结

旧的 `GroupDialogue` 通道**冻结**，只保留它已经修好的两个引擎门禁：

1. `_engine_blocks()` —— engine 不在 `DSH_ENGINES` 时整条通道不发；读不到引擎也当作
   不发（fail-closed，`group_dialogue.py:62-66`）。
2. `enabled()` —— 上面那一条 AND 上窗口开关与部署许可（`group_dialogue.py:68-71`）。

除这两处之外不再对 DSH 路径做非必要优化：不加新开关，不动生成、合并、摘要、逐段发送的
算法，也不为了"顺手统一"去改它的历史与任务存储。理由：这条路径即将退场，每改一处都要
重新论证一次"它不会在切换后多发一句"；冻结让第 1–3 节那些格子长期成立。需要改它时，先
让本文件里 engine 属于 {dsh,shadow} 的行仍然全绿，再谈改动。

## 9. 复跑

```powershell
$root='E:\Program Files\DSH\source\fish'; cd $root
$env:PYTHONPATH="$root\harness;$root\vendor\python;$root\tools"
$env:FISH_HARNESS_HOME=$root; $env:PYTHONIOENCODING='utf-8'
& 'C:\Python313\python.exe' -m unittest tools.test_send_matrix -v
```

必须逐模块单独跑（多个模块塞进同一进程会互相污染）。矩阵只看谓词：不建连接、不调模型、
不发 QQ 消息，每个格子用自己的一次性临时根目录。

## 10. 怎么读一个失败

失败信息形如：

```
engine=fish,listen=True,reply=False,reservation=active · live 立刻收回发送权
```

坐标给出引擎、两个开关、预约状态；后半句给出**哪一问**答错了（observe 收下 / 拿到 token /
live / enabled / 同游标再授予 / 旧 DSH 权限）。按坐标查第 1–3 节的表就知道预期，再按第 1 节
"负责的代码"列去读那一处生产代码。
