# Fish 实施对照表：代码位置、当前规则与退出清单

> 状态说明：第1—6节保留实施前定位快照，旧行号会随实施改变；当前工作树的新接口与退出状态见第7节。源码已改不代表已构建、载入或验收，运行结果以实施记录和最终07证据为准。

> 2026-10-24 本轮只读审查补充：第1—8节为各次定位快照，今日最新代码差异及未闭环问题见第9节。原30%预算、尚无目录/FTS等旧描述不再表示当前事实；需求仍仅由唯一规格负责。本轮只更新文档，未改运行代码。

日期：2026-10-24｜原 DeepSeek 合并任务表已按主人本轮裁决降级。

**本表仅记录代码事实与计划退出位置；需求和验收唯一依据为[实施规格](https://github.com/3259980040-droid/fish-agent/blob/codex/fish-tickets-20261025/project/spec.md)。** 行号是本次只读工作树的定位线索，实施时复核；“计划退出”不表示本轮已删除。正文不再另列目标、性能承诺或验收规则。

## 1. 实施前实际长度与节奏来源

| 当前来源 | 模块/存储与位置 | 当前值或实际含义 | 计划处理 |
|---|---|---|---|
| 表达规则 | `E:\Program Files\DSH\source\fish\harness\group_reply_style.py:3`、`:4`、`:7`、`:18` | MIN_CHARS=20、MAX_CHARS=500；通常范围是提示偏好，简单接梗可更短，讨论提示200—400，任务可超过；提示声明优先于人格一般长度偏好 | 只作为现状清点，不把数值写成新规格；保留唯一表达来源，变更时替代原值 |
| 普通主请求注入 | `E:\Program Files\DSH\source\fish\harness\fish_chat.py:337`、`:368` | 调用 expression_policy 并送入请求，证明上述模块参与当前请求组装 | 不再新增另一处长度提示覆盖 |
| 活动人格旧条目 | `E:\Program Files\DSH\source\fish\data\social\context.db`，default_personas(role=qq)及owner_routes.space→spaces.revision均指向同一personas；本轮revision为3dc6cef84d3946c79b030916faf77153 | 小鲸鱼·QQAgent卡仍有“群里闲聊默认1~10字”“默认1~10字”等偏好与一句一行等节奏条目；global_rules/workspace_rules无相应额外条目 | 在人格工单中列精确段落与版本差异，退出重复长度/失效工具指令；不直接删除人格特性 |
| 发送工具schema | `E:\Program Files\DSH\source\fish\harness\tool_host.py:230`、`:231` | fish_say每次1—3项文字，每项1—1000字符；是工具协议约束，不等于人格回复目标字数 | 如需调整在同一工具入口替代，不把它写成第二套表达规则 |
| 气泡与节奏 | `E:\Program Files\DSH\source\fish\harness\chat_output.py:12`、`:13`、`:16`、`:192` | 1—3文字气泡，最多1表情；后续气泡delay=min(4,1.2+0.035×上一气泡字符数)秒 | 保留其语义分段职责，不与表达重复定义总字数；本次不修改数值 |
| 通道分段 | `E:\Program Files\DSH\source\fish\harness\qq_agent_loop.py:1176` 一带 | 大于1800字符时按900—1800内的换行/句号或1800切段 | 仅为通道分段，不能误报成模型回答只能1800字 |
| 模型输出预算 | `E:\Program Files\DSH\source\fish\harness\model_gateway.py:16`、`:33` | max_tokens默认8192；本轮control无desktop_chat_max_tokens显式值，思考high。输出token非中文字数 | 参数归模型接入；本轮不改输出预算 |
| 旧代发工具单独限制 | `E:\Program Files\DSH\source\fish\harness\tool_host.py:306`、`:1312` | fish_group_send文本1—2000字符，仅是该旧工具检查 | 在工具/权限归并中判断替代；此次不得真实群发送 |

以上核实的是进入请求的来源和协议，并非本轮实测模型每次严格服从。运行代码和活动人格均未修改。

## 2. Agent、身份、上下文与前端位置

| 位置 | 当前职责 | 计划退出/合并 | 工单 |
|---|---|---|---|
| `E:\Program Files\DSH\source\fish\harness\fish_chat.py:176`、`:330` | 工具目录与主模型/工具循环 | 保留主入口，补能力；不建第二套执行器/登记 | 01、02 |
| `E:\Program Files\DSH\source\fish\harness\tool_host.py:375`、`:392`、`:446`、`:544` | 身份、space分支与执行 | 稳定主人/Agent/场所归属替代窗口凭据；保留统一执行 | 01、02 |
| `E:\Program Files\DSH\source\fish\harness\desktop_chat.py:55`、`:150`、`:168`、`:286` | 会话、人格和当前材料 | 退出会话决定授权/人格的旁路，入口适配接共同组装 | 01、03 |
| `E:\Program Files\DSH\source\fish\harness\desktop_context.py:27` | 局部历史、字符预算和压缩 | 统一到token预算与分层组装；不再各入口截条数 | 01 |
| `E:\Program Files\DSH\source\fish\harness\owner_private.py:45`、`:83` | 窗口有效性与私聊生成 | 退出窗口是否存在的授权前置，接共同经历 | 01 |
| `E:\Program Files\DSH\source\fish\harness\owner_private.py:131`、`:182`、`:203` | 回复、故障通知、处理 | 复用真实私聊发送/通知；提醒故障与自主不动分开 | 06、07 |
| `E:\Program Files\DSH\source\fish\harness\group_round.py:313`、`:659` | 群材料与回合 | 旧本群固定条数拼装退出，接统一材料；保留自主语义 | 01、06 |
| `E:\Program Files\DSH\source\fish\harness\workbench.py:745`—`:765`、`:811`—`:820` | 人格与任务管理 | 补活动配置/计划/状态的前后端同源；前端弃用动作逐项清理 | 03、06、07 |
| `E:\Program Files\DSH\source\dsh\packages\client\ui-fish\src\client\pages\FishTasksPage.tsx:266`、`:304` | 已有定时新建/删除 | 接聊天创建的同一任务；不再称全页只读 | 06 |
| `E:\Program Files\DSH\source\dsh\packages\client\ui-fish\src\client\pages\DelegationTasks.tsx:77`、`:86` | 委托取消/下载 | 使用真实任务与成果，不另造一份状态 | 02、07 |

## 3. 七类退出与保留职责清单

| 类别 | 源码定位 | 计划退出对象 | 保留/迁入职责 | 工单 |
|---|---|---|---|---|
| 主动重复 | `E:\Program Files\DSH\source\fish\harness\qq_agent_loop.py:352`、`:353`、`:369`；`E:\Program Files\DSH\source\fish\harness\group_proactive.py:117`、`:213`、`:277`、`:347` | 停用旧tick/独立生成发送与分散主动主线；不是整份文件先删 | 群回合仍用claim/evaluate/draft，先迁职责；提醒另行履约 | 06 |
| 冷却重复 | `E:\Program Files\DSH\source\fish\harness\group_policy.py:406`、`:440`；`E:\Program Files\DSH\source\fish\harness\group_decide.py:1384`、`:1417` | 重复否决及其叠加补丁 | 按最终职责只留一处决定；提醒不走自主冷却 | 06 |
| 资源关联重复 | `E:\Program Files\DSH\source\fish\harness\fish_library.py:36`、`:185`；`E:\Program Files\DSH\source\fish\harness\doc_store.py:203`、`:232`；`E:\Program Files\DSH\source\fish\harness\emoji_store.py:111` | 独立重复登记/索引与互不相认的存储入口 | 原件文件、文本块和图片资源有不同职责，迁到统一关联入口 | 04、05 |
| 视觉重复判断 | `E:\Program Files\DSH\source\fish\harness\multimodal.py:185`、`:201`、`:556`—`:557`；`E:\Program Files\DSH\source\fish\harness\vision_identification.py:61`、`:65` | 先核职责，再退出重复判断；当前已有直接调用证据 | 媒体准备/原图输入→VisionIdentification.resolve候选核对→结果回主模型；内部子模块可保留 | 05 |
| 三入口材料 | 上表owner_private、desktop_context/desktop_chat、group_round | 重复截取、拼接及入口性预算 | 来源适配、同一预算与共同经历 | 01 |
| 旧引擎/界面 | `E:\Program Files\DSH\source\fish\harness\qq_agent_loop.py:474`；`E:\Program Files\DSH\source\fish\harness\group_dialogue.py`；`E:\Program Files\DSH\source\fish\harness\app_v0.py` | 旧引擎分支、启动/发布引用，补齐替代后退出 | 仍使用能力先迁，不因文件名旧就提前删 | 02、07 |
| space授权 | 上表tool_host/desktop_chat；`E:\Program Files\DSH\source\fish\harness\memory_store.py:233`、`:266`、`:443` | space授权派生与运行时跨库归属查询 | 稳定主人/Agent/场所范围，历史引用仍可解析 | 01、04 |

## 4. 资料语义、调度与传输补建位置

| 位置 | 当前事实 | 计划删除/替代或补建 | 工单 |
|---|---|---|---|
| `E:\Program Files\DSH\source\fish\harness\doc_store.py:37`—`:39`、`:217`、`:239`—`:243` | 最新4000候选、前24词及前8粗筛，纯词法 | 删除时间先限候选及句末遗漏路径；补语义关联/明确降级 | 04 |
| `E:\Program Files\DSH\source\fish\harness\memory_engine.py:425`、`:524`、`:576` | 向量状态、查询与融合实现已在 | 补运行依赖/索引和UI真实状态；不可用明确词法，非空白 | 04 |
| `E:\Program Files\DSH\source\fish\harness\bot_control.py:205`、`:288`、`:355`、`:489` | 创建、更新、扫描和执行 | 提醒与自主意图区分，共用调度不共用“不动”裁决 | 06 |
| `E:\Program Files\DSH\source\fish\harness\memory_reminder.py:322`、`:414`；`E:\Program Files\DSH\source\fish\harness\private_proactive.py:206`、`:278` | 到期提醒与私聊主动各自入口 | 分别映射提醒履约/自主意图，退出重复入口，不把提醒吞进自主沉默 | 06 |
| `E:\Program Files\DSH\source\fish\harness\dsh_web.py:49`；`E:\Program Files\DSH\source\fish\harness\attachment_store.py:27`；`E:\Program Files\DSH\source\fish\harness\tool_host.py:1745` | 不同传输和附件大小来源 | 不用的订阅入口退出；原件按资源处理，不统一乱放大 | 08（依赖 02、04，交 07 集中验收） |

本表本轮只做只读定位与文档改写，尚无运行源码删除或迁移。

<a id="module-interface-map"></a>

## 5. 模块接口映射（codebase-design：真实接缝与迁入位置）

本节为本轮只读源码定位。固定引用为“模块接口映射”（锚点 module-interface-map），章节编号不作为工单定位依据。目标接口和依赖以权威规格 4.10 为准；下列“拟处理”没有在本轮实施，也不证明线上当前已走到相应分支。

| 源码事实与位置 | 说明 | 拟处理／工单 |
|---|---|---|
| `E:\Program Files\DSH\source\fish\harness\fish_chat.py:330`；`E:\Program Files\DSH\source\fish\harness\owner_private.py:83`；`E:\Program Files\DSH\source\fish\harness\desktop_chat.py:286` | 主循环已有；调用者仍分别准备 persona、history、memory、scope、references、current、工具面和输出形式等十余项材料 | 深化现有主循环与共同组装接口，入口只交唤醒引用／取消信号；不在外面再加一层照传旧参数的壳。01、02 |
| `E:\Program Files\DSH\source\fish\harness\fish_chat.py:18`、`:176`；`E:\Program Files\DSH\source\fish\harness\tool_host.py:208`、`:386`、`:544` | 主聊天 CATALOGUE 另列工具名，tool_definitions 从 builtins 建表后再按名单取；公布目录与执行器内部窗口／模式分支仍是两层知识 | 以统一登记生成可发现目录，退出手抄工具名称筛选及窗口凭据派生；保留真实来源、目标与结果，不新增权限门。01、02 |
| `E:\Program Files\DSH\source\fish\harness\desktop_context.py:27`；`E:\Program Files\DSH\source\fish\harness\group_round.py:313` | 前者自行字符计量、同步续聊摘要；后者自行按群字符预算倒序选消息 | 材料归同一预算和覆盖范围；保留各入口接收／媒体适配，退出各入口自定预算和截取。01 |
| `E:\Program Files\DSH\source\fish\harness\persona_library.py:175`；`E:\Program Files\DSH\source\fish\harness\workbench.py:745`、`:761` | 人格 apply 仍通过创建／切换 space，页面应用后又可 reload 当前桌面对话 | 保留预设库及既有页面动作名称可用部分，活动人格解析归运行设置；退出窗口决定活动配置和能力的旁路。03 |
| `E:\Program Files\DSH\source\fish\harness\bot_control.py:104`、`:115`、`:147`、`:205`、`:226`、`:270`、`:355`、`:477`、`:489`、`:531` | 已可注入 transport、on_fire、session_factory、run_turn；默认会创建 DSH 会话，计划 CRUD／扫描读取 DSH 原生计划，执行器也连接 DSH | 复用本地计划／实例和可用时间计算，将登记、到点和执行改接 Fish；退出 DSH 会话工厂与远端计时读写，不能仅换 run_turn。06 |
| `E:\Program Files\DSH\source\fish\harness\bot_control.py:541`、`:546` | _send_count 当前把 SUCCEEDED 与 EXECUTING 都计作发送数，_latest_receipt 只取 SUCCEEDED；两个口径不同 | 用真实投递结果完成任务，执行中不能升级为送达；复用既有状态含义，不建第二套枚举。06、07 |
| `E:\Program Files\DSH\source\fish\harness\private_proactive.py:139`、`:153`；`E:\Program Files\DSH\source\fish\harness\memory_reminder.py:172`、`:204` | 提醒与主动 compose 都用空历史、空记忆及无工具形式调用主函数；异常会退模板 | 接带意图的同一唤醒、共同材料和工具；退出空经历旁路与掩盖模型失败的自主话术模板；提醒按规格单独履约、故障单报。06 |
| `E:\Program Files\DSH\source\fish\harness\group_round.py:469`、`:489` | 生成后仍调用 worth_speaking，再作发送判断；冷场还用 proactive.draft | 随主线归并迁移仍使用职责，退出旧决定链；维度引导的组织比较留在后续 G7，不在这里新增独立判断器。06 |
| `E:\Program Files\DSH\source\fish\harness\model_gateway.py:184`、`:215`—`:217`、`:235`—`:247`；`E:\Program Files\DSH\source\fish\harness\workbench.py:431`—`:510` | 流已读 reasoning_content，仅 tools 存在时收集；工具调用结果会返回该字段，普通 stop 结果只返回正文。工作台现有痕迹还从请求／工具记录按时间归集 | 在模型接入处保留并传出真实可用字段，以同回合关联显示；退出根据零散时间推定的假对应及额外解释生成，不要求再调用模型写理由。07 |
| `E:\Program Files\DSH\source\fish\harness\qq_agent_loop.py:1152`；`E:\Program Files\DSH\source\fish\harness\owner_private.py:131`；`E:\Program Files\DSH\source\fish\harness\chat_output.py:29`、`:66` | 已有实际投递、语义气泡和正常不发送语义；客户端目前 _chat_turn 默认纯文本呈现，QQ 依发送工具队列 | 在现有投递与本地呈现处接同一行动／结果接口，继续区分待发与确认；不另建发送队列或让入口再次判断她该不该说。02、07 |

可沿用的外部接缝证据：`tools/test_owner_private.py` 已替换 ModelGateway 与出站 action；`tools/test_turn_sending.py` 已有 FakeChannel；BotControl 的注入参数可用于临时根出站。这里只读这些入口，没有运行测试或增加测试文件。它们服务于规格的集中整体观察，不成为逐票实发／付费测验要求。

传输与大附件归 08：调用资源登记／原件接口，具体当前值与旧路径见下节；07 等 08 完成后集中验收。

资源位置继续沿用第 3—4 节：原件／资料块／图片是资源模块内部不同实现，媒体准备仍实际调用专业视觉核对；按职责合并引用，不能整个文件先删。


## 6. 传输层与大附件：当前事实和工单归属

本节为本轮源码只读结果。下列均为当前代码值，不是新的产品上限；实际更改后在本表原位填写新值和作用域。目标与验收只由权威规格 4.8、5.2 决定，交付归 08，M01 不负责这一能力。

| 层／位置 | 当前事实 | 迁移／替换对象 |
|---|---|---|
| `E:\Program Files\DSH\source\dsh\packages\client\ui-fish\src\client\pages\FishChatPage.tsx:480`、`:487`；同目录上层 `locales.ts:169`、`:583` | 选择文件检查 20 MiB，整份编码后调用 attachments.import；中英文文案也写 20 MiB | 前端接分块／流式导入及真实配置，退出重复硬编码与整份大 base64 请求 |
| `E:\Program Files\DSH\source\fish\harness\workbench.py:835`—`:845` | attachments.import 检查 base64 字符串长度 28×1024×1024，再整体解码送 import_bytes | 附件内容不再借普通动作 JSON 整份内联；保留同一原件／资源关联，不能只放大字符串检查 |
| `E:\Program Files\DSH\source\fish\harness\tool_host.py:1745` 一带 | Content-Length：/workbench 32 MiB，其他路由 2 MiB；先整份读取 JSON | 控制消息大小与原件传输分层，附件路径适配流式／分块；普通控制路由不统一升到 1 GiB |
| `E:\Program Files\DSH\source\fish\harness\desktop_chat.py:221` | 本地文件另检查 20 MiB，再走 AttachmentStore.import_path | 退出与新导入规则不一致的重复大小判断 |
| `E:\Program Files\DSH\source\fish\harness\attachment_store.py:27`、`:28`、`:221`、`:258`、`:298` | MAX_BYTES=20 MiB；EXCERPT_BYTES=256 KiB；文本仅解析开头；本地路径 read_bytes 后整体导入 | 原件分块落盘、统一登记／资源引用，按需读后续片段；明确解析范围，不因只索引开头就宣称读完 |
| `E:\Program Files\DSH\source\fish\harness\dsh_web.py:49` | FISH_DSH_FOLLOW_MAX_MIB 默认 1024，即该旧订阅连接 1 GiB；它不控制附件导入或模型窗口 | 仍被调用时注明本层作用；旧 DSH 订阅退出时一同退役其无效配置，不移植成全局附件上限 |

08 实施后记录“旧值／新值／作用层／读取配置处／前端显示来源／实际覆盖范围”。超过旧 20 MiB 原件的导入取用在最终同一次体验中观察；本轮没有执行文件上传、生成大测试文件或做传输压力实验。


## 7. 本轮新源码落点（尚未载入运行）

| 职责 | 当前工作树接口 | 已退出的重复路径 | 仍待交付证据 |
|---|---|---|---|
| 共同经历 | harness/experience.py:ExperienceTurn；desktop_context.py:history；context_compaction.py:tick | 回复前安静窗口、群_focus独立拼历史、旧字符预算 | 共同视图与自主回合归并、整体运行 |
| 真实预算 | harness/context_budget.py:ContextBudget.fit/snapshot；fish_chat.py:respond | 各入口另算字符/条数作为总预算 | 实际请求/usage/五时间点对应 |
| 人格 | harness/social_context.py:active_persona/apply_active_persona；ui-fish FishPersonasPage | 主人下一轮依赖历史窗口绑定、预设仅保存不可选 | 生产选择迁入与保存/应用实际往返 |
| 工具 | harness/tool_host.py:builtins/execute；agent_tools.py；fish_chat.tool_definitions读取/catalog | 主聊天CATALOGUE手抄白名单、插件按send/post名称默认拒绝 | 能力完整对照、子任务成果、实际工具回执 |
| 资料 | harness/doc_store.py:ingest/search/doc_detail/maintain；fish_library仅适配归档入口 | 新网页JSON旁注第二索引、前8/24词与最新4000候选过滤 | 旧资料迁入、向量实用、原件/检索UI往返 |
| 记忆 | harness/memory_store.py:get/history/recall；memory_engine.py；memory_embeddings_worker.py | 主人记忆读取依赖spaces来源、语义仅看首1000/4000字符、旧跨角色FAISS快捷路径 | tools/migrate_agent_environment.py实际备份/迁移，纠正后召回 |
| 视觉 | harness/image_views.py:register/view/preview；multimodal.py:_image_uri；tool_host.image_look | 一律1440缩图作为模型输入、自动二次角色核对、缓存描述冒充新图 | 原图/局部实际进模型、界面构建、GIF原件私聊 |

专业候选核对仍在vision_identification.py:inspect/analyze，只经fish_image_verify明确调用，返回辅助候选数据；不是每张图自动跑的最终判断。显示预览缩图与模型视图是两个不同用途。

传输层当前20MiB等实际限制仍由08继续替换，不因资源索引能记录原件就宣称大附件已通。06的旧定时/主动路径、07状态与最终私聊过程仍未实施完成；M01仍延期。工具来源关系见实际根docs/agents/tools.md，未声称已迁齐DSH全量能力。


### 2026-10-24：06源码接线补充（未激活/未验收）

- 唯一计划与到期执行：`harness/fish_schedules.py`；唯一扫描/唤醒队列：`harness/agent_scheduler.py`。管理入口`BotControl`转发到它们，不创建DSH计划/会话。
- 模型和工具：仍为`fish_chat.respond`；到期委托`delivery_required`复用现有输出契约，不叠加第二套长度规则。
- 发送分段唯一函数：`chat_output.transport_parts`；实际通道仍是原fish-qq，必须实际消息编号才标送达。
- QQ输入：`QQAgentLoop._on_group/_fish_start`先存原消息再由群队列积累，已考虑范围来自`experience.py`，没有安静等待窗口与外层25秒付费线程弃等。
- 删除/退役：GroupDialogue/Shadow实际入口、付费预筛/二次否决、旧主动三个入口与旧跟进等待器退出；旧模块已移项目需要清理目录，原历史库保留。旧群参数界面清理仍待07。
- 激活前：执行`tools/migrate_fish_schedules.py`确认旧调度器归属已退出；随后统一构建/重载/单次私聊与客户端验收。源码接线不算真实生效。


### 2026-10-24：08源码接线补充（未激活/未验收）

- 原件/分块/绑定/读取范围：`AttachmentStore`；统一资源/切片/检索：`doc_store`。旧附件FTS保留历史表，退出新写入和查询。
- 前端聊天、资料库唯一分块函数：`ui-fish/src/client/attachments.ts`。后端`attachments.config/begin/chunk/finish/detail/open`同一入口。
- 旧20MiB与28MiB检查退役；HTTP控制限制由`tool_host.control_ceiling`负责；图像协议由`ImageViews.NATIVE_IMAGE_BYTES`负责；原件不整份成为模型输入。
- 新增`fish_attachment_read/search`按实际范围读取/搜索原件，实际取用写附件读取记录。
- 成果归档/下载/QQ投递取现有原件，不整份base64复制。旧DSH订阅1GiB仍由未收口的TaskDelegation使用，不混作文件/模型承诺。
- 实际旧QQ附件5份原件均在；迁移脚本默认只读、应用前备份两库，尚未应用。验收统一在07。

<a id="workbench-operation-map"></a>

## 8. 原生操作工作台：接口与退出对象映射（09，只读定位）

日期：2026-10-24。本节只给已读代码、缺口与拟退出对象；需求和验收仍由权威规格 4.7、5.6 决定。本轮未实现 09、未调用生产模型或管理接口，以下“待补接”不等于线上已有。

| 职责/真实位置 | 本轮核对事实 | 09 接法与退出对象 |
|---|---|---|
| `harness/workbench.py:642` | delegations.list/submit/get/cancel/open/download 已有 | 执行任务详情接真实结果与成果；退出页面独立推断的成功状态 |
| `harness/workbench.py:673`、`:721`、`:764` | settings.get/save、channels.status/update 已有；现有设置保存只覆盖其明确字段 | 读取真实配置并按项保存；模型服务管理先找正式配置入口，不往 settings.save 塞一份无运行读取的页面配置 |
| `harness/workbench.py:783`、`:797`、`:804`；`DSH宿主/ui-fish/src/client/pages/FishPersonasPage.tsx:132` | 人格列举、保存、应用入口已有；saveDraft 后 load 会从活动人格重装编辑内容 | 区分保存后的选中预设与当前活动版本；退出保存后被旧正文覆盖的交互，不另造人格存储 |
| `harness/workbench.py:806`—`:848` | memory 的 overview/curate/digest/history/search/save 已有；digest.revise 为模型按意见修订；工作台当前分支未暴露删除/合并动作，删除实现已在 MemoryStore.delete | 现有动作与删除方法直接接管理流程；合并需补到资源与记忆，保留来源关联；退出仅时间线、假列表删除和将模型改写当直接保存 |
| `harness/workbench.py:850`—`:859` | schedules.list/create/update/delete 已有；当前已读分支未见 schedules.run，但 BotControl.run_now 和 FishSchedules.run_now 均已存在 | 工作台补最小桥接到既有 run_now；核对暂停计划人工实例的领取语义；退出只读任务页，不造第二调度器 |
| `harness/workbench.py:975`—`:1003` | library.status/open/organize/upload/list/detail/delete/search 已有；upload 直接为文本 ingest | 接资料对象管理、真实试查与导入进度；文件沿现有 attachments 分块原件通路，不把文本表单等同全格式解析 |
| `harness/plugin_registry.py:77`、`:89`、`:93`、`:163`、`:182`、`:348`、`:382` | 已有 PluginRegistry 的 entries/save/toggle/remove/discover/catalog；该事实不证明工作台已有完整管理桥接 | 核其他正式入口后复用这一注册表接插件/API/MCP；不在前端另存第二份工具清单；当前无通用热更新实现证据时不宣称所有插件支持 |
| `DSH宿主源码/packages/client/ui-plugin-manager`、`ui-plugin-market`、`ui-skill` | 组件包存在；当前未证明可直接管理 Fish 模型/插件/Skills 来源 | 复用合适表单和编辑器，数据绑定 Fish；模型用途/Skills 管理先核正式配置和加载路径，确缺才补对应桥接 |
| `DSH宿主源码/packages/client/ui-sidebar-files`、`ui-sidebar-terminal` | 已有文件与终端界面包 | 接 Fish 实际工作区/交办工程与任务，不沿用 DSH 当前项目默认值，不另建客户端或搬工作根 |
| `DSH宿主/ui-fish/src/client/pages`、`pages.module.css`、`FishPanel` 与 `locales` | 现有 Fish 页面和原生宿主集成位置 | 在此演进导航、对象列表/详情、操作及反馈；迁移完成后退出对应旧路由、重复控件和无用文案；具体删除记录实施后补齐 |

拟退役的设计规则：固定六页、每页仅一个操作、灰字只能空/错、错误只能到设置看、为“只改前端”而保留空按钮。当前没有删除任何生产源码；本节列的是 09 实施对象，不以文档改写充当源码清理完成。

本次源码事实只限已读入口；没有定位到工作台动作不等于整个项目不存在该能力。开始实施前扫描最新工作树，优先重用；实现与验证结果在 09 的实施记录补记。

### 8.1 codebase-design：实际深化位置与 Interface 核对

下列为同日追加只读定位；目标仅由权威规格 4.10.5 定义，未修改这些实现。

| 源码证据 | 当前事实 / 深化位置 | 09 要退出的散落知识 |
|---|---|---|
| `DSH宿主/ui-fish/src/client/workbench-client.ts:createWorkbench`；`src/index.ts:apply/forward`；`harness/tool_host.py:1533`；`harness/workbench.py:dispatch` | 浏览器 → 宿主同源转发 → Fish工作台分派的真实 Seam 已有；宿主包含启动/维护转发，不能将它另作 Agent 核心 | 不再造第二条直连通路或万能 WorkbenchCore；不在宿主重算人格、计划、记忆或工具决定 |
| `DSH宿主/ui-fish/src/client/FishPanel.tsx:requestRefresh`、`pageProps`；`page-props.ts:FishPageProps` | 各页接 raw call/reportRefresh；外框每切页读全局 status，各页操作又分别读/解析/刷新；这不是全部后端状态都由外框维护的证据 | 对象 Module 集中操作顺序与结果；外框只留全局摘要；对象读写定向刷新，不另建一个全局包揽业务的状态仓库 |
| `DSH宿主/ui-fish/src/client/pages/FishPersonasPage.tsx:load/saveDraft/applyDraft/applyRevision` | load 同时 setCurrent 与 setBody；saveDraft 保存后调用 load，草稿被活动人格覆写；保存并应用的两个阶段由页面直接编排 | 草稿/已保存/活动分别关联；保存应用和部分成功集中在人格编辑 Module；退出页面各自重写的保存刷新顺序 |
| `harness/bot_control.py:130`、`:198`；`harness/fish_schedules.py:222`、`:231`、`:235` | 已有 run_now 创建 due 实例；pending/claim 排除 paused，单纯桥接按钮不保证暂停计划能手动运行 | 复用实例和调度；人工立即运行与暂停状态的接续在计划 Module 内实现，不临时恢复再暂停，不另开执行线程/计时器 |
| `DSH宿主/ui-fish/src/client/pages/FishTasksPage.tsx:dueText/firstField/statusDot` | 页面猜多个时间字段、状态映射；有效表单转换也在页内 | 在计划操作 Module 的 Interface 适配一次；不把业务原始记录复制成第二库，不强制所有格式化函数独立成文件 |
| `harness/memory_store.py:385`；`harness/bot_control.py:204`—`:220` | MemoryStore.delete 已删除活动条目/向量并按既有参数登记防重学；工具管理已有调用；不证明工作台删除/合并已接通 | 工作台走原存储方法；合并保留来源与修订关系由原 Module 内补，不在页面或工作台再写一遍 DELETE |
| `harness/core.py:978`；`harness/app.py:410`、`:426`；`harness/setup_config.py:save_setup`；`harness/model_gateway.py:ModelGateway.__init__` | 现有角色配置保存、保护凭据和主模型读取已有；setup保存同时涉及DSH CLI/QQ/人格等，不能直接当作仅改模型连接的 Interface | 在运行设置内保留同一有效模型/密钥来源，按用途补接；新连接表单不调用整套初始化向导，不顺带改人格/DSH配置，不建一份没人读取的 model-providers 页面 JSON |
| `harness/plugin_registry.py:PluginRegistry`；`DSH宿主源码/packages/client/ui-skill/src/index.ts` | Fish 已有插件登记/发现/执行；ui-skill 节点侧是纯UI入口的空apply，该事实不能证明其已管理Fish技能加载 | 扩展页复用Fish注册；Skills接实际Fish加载 Interface，缺口如实补；不把DSH界面包存在当作后端接通或借用DSH人格加载 |
| `harness/agent_tools.py:AgentTools.workspace/execute` | 文件读写、目录、workspace、shell/process已有，工作目录按真实任务归属；前端工作区尚待接通 | 文件和进程来自既有 Module；不复制一套网页文件服务器，不把DSH当前项目当作Fish默认根 |
| `DSH宿主/ui-fish/src/client/parsers.ts`、`contract/workbench.ts`、`contract/slots.ts` | 已有传输结果与对象解析；旧mode/work语义仍能在类型中看到，这只是现状而非本轮允许重新引入工作模式 | 新页面只依赖真实需要的对象 Interface；迁移完成时清理失效解析/文案及仅锁死旧布局的断言，不从旧类型自动生成新产品开关 |

删除判断：传输 Adapter 只搬运数据，保持小且清楚；人格编辑/计划管理 Module 则应消化保存应用顺序、草稿关联、时间选择与执行实例拼接。若只加一层原样转发，不能认作完成深化。未被证明失效的独立行为检查和历史数据继续保留。

本轮没有跑付费模型、注入生产故障或做新测试矩阵；依赖与整体观察按权威规格 4.10.5、5.6。只完成代码阅读与文档设计，09状态不变。

### 8.2 to-tickets：09子票的落点分工（未实施）

本节只维护“由哪张票修改哪类现有职责”，不新增权威需求。09父规格保留原文；真实领取、依赖和状态以本地工单索引为准。源码证据继续用8.1，领取时核当前工作树，不以旧行号作为另造实现的理由。

| 子票 | 主要落点/负责Module | 用户故事编号 |
|---|---|---|
| 09-01 | 现有FishPanel导航/主题、人格页面与运行设置；首个完整过程内整理编辑接续 | 58、59、60、61、62、63、64、65、66、67、101 |
| 09-02 | 现有任务页面、工作台schedules桥接、BotControl/FishSchedules和实例详情 | 61、62、63、68、69、70、71、72、73、101 |
| 09-03 | 现有有效配置/凭据保存、模型读取和用途绑定；模型服务视图 | 62、63、78、79、80、81、100、101 |
| 09-04 | 现有PluginRegistry、工具目录和执行；扩展/API/MCP管理视图 | 61、62、63、82、83、85、101 |
| 09-05 | 核现有Skills加载及宿主编辑视图；Fish实际装载/管理的最小交接 | 61、62、63、84、85、101 |
| 09-06 | MemoryStore与既有小结/社交语料管理，工作台对应动作及记忆视图 | 61、62、63、86、87、88、89、94、101 |
| 09-07 | doc_store、现有原件/附件/网页导入及检索，资料管理视图 | 61、62、63、90、91、92、93、94、101 |
| 09-08 | AgentTools的文件/workspace/process及宿主文件终端视图 | 61、62、63、74、75、76、77、101 |
| 09-09 | 既有settings/channels/故障记录与宿主维护动作，连接系统视图 | 62、63、95、96、97、101 |
| 09-10 | 既有聊天/附件/trace/任务成果交接与投递展示，整体连续体验和旧入口汇总 | 58、60、62、73、75、97、98、99、100、101、102 |

同一负责Module可能被多票使用；修改内部Interface含义时同步真实调用者，不在每票追加一个同名Facade。原01—08不重做，本轮没有修改运行代码、调用模型或执行验收。

## 9. 2026-10-24 今日改动与09的只读复核

完整证据：Codex只读审查报告（历史记录仅留本机）。本节只记录当前事实、负责位置和计划退出对象，以下未修复。

| 当前源码/对象 | 已核实事实 | 负责票与替换去向 |
|---|---|---|
| harness/desktop_context.py:19,214；context_budget.py:53 | 已删除旧字符常量，按token选最近材料；两处配置来源/窗口解析不一致 | 01：同一有效配置/当前模型解析，替换缓存最大容量与固定猜值 |
| harness/experience.py:43 | 多来源older剥掉来源取最大数字，再向当前来源/会话排队 | 01：按真实来源和范围，不保留裸编号决定 |
| harness/context_compaction.py:76,85,103,147,180,214,220 | 已加checkpoint/catalog，但自动开关非全入口、失败可重复pending、超大条假进度、旧摘要截尾和多次提交仍在 | 01：迁用DSH选择/合并/提交逻辑，原辅助执行槽保留；退出被替代分支 |
| harness/history_index.py:35,58 | 两来源已建FTS；只追加MAX(rowid)之后，search尚无真实调用者 | 04：接实际检索与更新/删除随动，不再造另一FTS实现 |
| harness/history_catalog.py:18,43 | 目录查询有实现但不返回source；原文SQL假定QQ字段，桌面库缺这些列 | 04：统一来源适配与引用/分页，目录→原文闭环 |
| harness/tool_host.py:325,738,750,962 | 新历史工具有schema但外层名字集合不包含它们，现分支不可达 | 02接同一分发，04保留读实现；不复制执行器 |
| harness/tool_host.py:213,1658；fish_chat.py:29 | 默认22工具和MCP隐藏；主请求只读tools且没有补齐隐藏工具的发现入口 | 02补完整动态发现；09-04只管理同一注册，不存第二白名单 |
| FishTasksPage.tsx:285,371；workbench.py:850；fish_schedules.py:222,231 | 当前页面已有新建/编辑/暂停恢复/删除；run_now基础存在，暂停实例被pending/claim排除 | 09-02：桥接立即运行、手动实例与真实结果，退出重复字段猜测 |
| core.py:978；ModelGateway.__init__；SecretStore | 模型配置读写和凭据基础已存在；缺完整连接/用途管理界面 | 09-03：单一有效来源，必要连接对象迁移消费者，不直接新建无人读取models.json |
| plugin_registry.py:PluginRegistry.save/discover | 插件登记/发现已存在，不只是catalog；工作台管理仍缺 | 09-04：复用原注册与生命周期；技能真实加载独立由09-05核对 |
| memory_store.py:385；FishMemoryPage.tsx:365 | 长期记忆delete已有；页面停用调用language.manage，针对语料 | 04/09-06：接原删除与新增停用/合并；不混淆两个对象 |
| workbench.py:916,934；FishChatPage.tsx:155；turn_trace.py | 已读真实思考；列表只挂载加载，错误清空；正文截20000字；trace本身保存更多可用内容 | 09-10：唯一trace引用、刷新、完整读取/分页与中文错误，不生成解释 |
| governance.py:220,299 | config读取并归一，save才写文件；非治理键可能在save时丢弃 | 原负责配置模块+09-09：独立职责、单一来源，纠正“读取会重写”说法 |

库结构只读核对确认：桌面messages使用created_at，无QQ的ts/chat_id/sender_id。旧聊天已清空后有新消息自然进入，旧trace仍保留；不恢复旧数据、不用清空前统计冒充本轮验收。

任务入口仍为原工单及索引的[本轮分工](https://github.com/3259980040-droid/fish-agent/blob/codex/fish-tickets-20261025/project/ticket-index.md#review-20261024)。前端与后端分开，历史票状态不自动消除新开放问题。

<a id="codebase-design-20261025"></a>

## 10. 2026-10-25 codebase-design：当前事实与目标源码映射

唯一行为与Interface设计见[权威规格4.10.6](https://github.com/3259980040-droid/fish-agent/blob/codex/fish-tickets-20261025/project/spec.md#agent-structure-20261025)，目标来源见同规格7.6；责任见[工单索引](https://github.com/3259980040-droid/fish-agent/blob/codex/fish-tickets-20261025/project/ticket-index.md#confirmed-20261025)。本节只维护源码事实、建议位置和迁接/退役，不另立产品规则。

本轮直接读取Fish工作树HEAD `5abfe0d`，读取时后端及DSH的ui-fish子树未显示未提交改动；这是读取时的源码状态，不是生产进程版本证明。前文第9节为2026-10-24旧事实，已有修复以本节核对为准。未导入生产写库类、调用模型、测试QQ、修改代码或重启。

### 10.1 已有能力及真实旧调用者

后端源码根：`Fish仓库根`；前端根：`DSH宿主/ui-fish`。下列行号定位本次工作树，实施时按方法名复核。

| 已核实源码/入口 | 当前事实 | 目标迁接与退役归属 |
|---|---|---|
| `harness/unified_service.py:main`；`fish_desktop/services.py:573 start_fish`；`harness/tool_host.py:1782 serve` | Fish后台宿主创建ToolHost，再由services启动fish-qq和qq-agent-loop子进程；桌面执行和QQ执行不在同一Python进程，进程内单例无法自然统一它们 | 装配处创建唯一主执行并注入ToolHost/Workbench/计划；QQ进程保留收发Adapter，提交到同一宿主；启动/关闭关系一起迁，不在QQ进程再建runtime |
| `harness/qq_agent_loop.py:on_event`、`:418 _on_group`、`:431 _fish_start`、`:556 _fish_reply`、`:755 _worker` | 私聊按item进入queue/_worker；群观察后每次调用_fish_start，已有按群合并的inbox/pump，并非每条群消息各建一个付费回合；群/私聊仍分别准备与执行 | 01/06：保留接收/QQ来源记录及必要通道操作，移走私聊worker和每群pump的模型执行职责，统一submit；新增@/8条/45分钟目标判定在主执行，不能从当前行为反推目标 |
| `harness/owner_private.py:72 generate`、`:115 reply`、`:193 process` | 私聊自行读取资料/人格、构造回合并调用respond；系统故障通知复用reply | 01/06：generate/process的主执行和组装迁入；reply中的真实通道投递迁投递Module。故障通知仍明确为系统通知，保留其必要真实出站，不让整个文件因移走聊天逻辑而误删 |
| `harness/workbench.py:209 _executor`、`:1178 chat.send`、`:_send`；`harness/desktop_chat.py:197 receive`、`:230 send`、`:328 _chat_turn` | 客户端已有request_id去重、先receive再独立线程队列send；桌面内部另外准备媒体/记忆并调用聊天或work旧路径 | 01/09-10：保留真实接收与请求引用，工作台chat.send改submit同一主执行，chat.job由原引用读结果；退出桌面独立生成队列、入口自拼材料及目标已停用的工作模式路径。既有历史浏览/保存不丢 |
| `harness/group_proactive.py:50 request_self_wake`、`:88 _take_self_wake`、`:212 claim`、`:262 mark_sent`、`:468 tick` | 自唤醒chain跟随待执行行，take时删行；claim仍按last_attempt/随机间隔/退避，最后还经过evaluate；并非目标的全局开始清计数、完成重计时 | 06：将有用工具参数接主执行request_followup；持续次数和截止归主执行状态。旧随机节拍、fresh资格、重复evaluate/tick退出目标触发主线；不得把当前延迟3—600秒当目标参数 |
| `harness/tool_host.py:641 fish_think_again`、`:929 fish_memory_search`—`:981 social` | 自唤醒当前只接群scope；群memory_search仍固定本群，complete/delete仍拒群；只修改save不足以统一 | 02/04/06：工具登记与执行仍同源，调用对应公共Interface；统一记忆/续醒不再被这些入口分支改义。身份来源保留，不新建一套“开放版”工具表 |
| `harness/agent_scheduler.py:AgentScheduler.loop/_run`；`harness/fish_schedules.py:235 claim`、`:255 run`、`:306 _thought` | 独立线程池直接plans.run/_thought后调用respond；保存了明确计划/实例及旧两小时/07点/22点初始化计划 | 06：计划保留save/detect/occurrence，due改提交主执行，不自行生成；旧两小时初始化默认巡查由新45分钟默认触发替代，保留明确用户计划及既有早晚问候实例语义，避免两套常驻巡查 |
| `harness/storage.py:180 thinking_slot`；`harness/fish_chat.py:168 respond`、`:211 _respond`、`:270 experience.updates` | FIFO/文件锁是当前多入口串行化；工具接续前会加入新消息 | 01/06：单消费主执行拥有回合；有用模型/工具协议循环迁入execution内部，新消息按已定回合快照进入后续统一回合。退出把多个入口都挂到同一锁上的主调度做法；压缩辅助槽不回收为主聊天锁 |
| `harness/experience.py:ExperienceTurn`、`harness/desktop_context.py:DesktopContext.history` | 已有共同取材、原始来源/位置、已考虑记录、五时间点基础；已考虑消费仍带旧session入口语义 | 01：迁入experience，保留原始引用及计量；加入主输入/背景区分、统一接收顺序及快照，退出以旧场所session作为不同执行者的消费方式 |
| `harness/context_compaction.py:49 _record_checkpoint`、`:159 tick`、`:190—241` | 已用experience.db保存checkpoint/catalog/compactions；仍先写原库chat_summaries，再写检查点，再提交processed；旧摘要截尾及输入8000补丁仍在 | 01：迁用DSH选段/合并/提交，检查点/目录/进度同库同提交；旧chat_summaries停止成为活动写源。保留原始内容和历史可定位，不新建平行摘要器 |
| `harness/history_index.py:35 ensure`、`:62 search`；`harness/tool_host.py:780/810`；`harness/history_catalog.py:read_range` | FTS5已存在，工具实际调用search，search已有桌面字段适配；ensure已删除源不存在索引，但仍按MAX(rowid)追加，旧行文字更正不会更新；不能继续说“search无调用者/桌面必报缺列” | 04：保留实际查询，迁为来源写入/修订驱动的索引随动，目录→概述→原文继续收拢；不重写已经修复的适配，不再造第二张同义历史索引 |
| `harness/memory_store.py:233 _visible_scopes`、`:276 save`、`:385 delete`、`:811 enqueue`、`:923 learn_one`；`harness/memory_engine.py:560 recall` | owner读owner/public，群读本群/public，向量召回也依赖该范围；正文、修订、防重学和学习队列已有 | 04：统一生产Agent读视图及相关get/save/修订/语义/小结调用；旧scope转来源用途，生产记录ID保留。现有学习和来源证据迁入共用整理程序，不只改工具一处search |
| `harness/tool_host.py:1758 worker`、`:1831—1837`；`harness/qq_agent_loop.py:331 _maintenance`；`harness/memory_maintenance.py:528/731/1055`；`harness/daily_curate.py:276/306` | host worker每2秒learn_one/压缩/索引；另有MemoryMaintenance后台每日/周学习；QQ维护另调daily_curate清理/小结/表情；页面又可直接run，实际有多种启动和消费 | 04/06：一个organize任务入口与单一整理程序；既有取材、学习、修订、摘要、表情子步骤迁入，移走重复定时检查/消费者；压缩归experience，不继续寄宿learning worker |
| `harness/attachment_store.py:103/166/173/247/260/280`；`harness/multimodal.py:125/405/454` | 已有原件登记、分块上传、解析/片段读取与MediaService；没有此次目标的连续3秒批次Implementation | 08：复用原件与媒体，实现experience内部batch关联/封口/交接；下载/解析在knowledge内部，不能另造附件库或让每个入口分别计三秒 |
| `harness/bot_control.py:292 deliver_bubbles`、`:306 _deliver`、`:357 _channel_transport`；`harness/qq_agent_loop.py:_deliver_reply`；`owner_private.reply` | 计划/主动/私聊存在不同投递编排，底层均有真实通道回执和现有气泡/段落处理 | 02/06：把共用气泡、目标、原件及回执编排迁投递Module，保留实际QQ传输Adapter；旧调用者仅交发送意图，不各自再次发送或重复判成功 |
| `harness/workbench.py:878 memory.curate.run`、`:904—955`；前端`src/client/pages/FishMemoryPage.tsx:154/239/250/278/309`、`FishTasksPage.tsx:365` | 工作台已有记忆停用/合并/保存和schedules.run→run_now；页面已有相应调用。不能按旧审查把这些全部当作缺失；但它们仍读owner范围，curate当前同步run | 09-06/09-02：消费统一知识/整理进度和计划实例Interface，复用有效操作；页面不从翻译后的状态文案反向决定业务状态，不复制索引/计时；run_now的暂停实例语义仍需依其执行路径解决 |
| 前端`src/index.ts:145/183`、`src/client/workbench-client.ts:45`、`FishChatPage.tsx:161/482`；后端`turn_trace.py` | 已有宿主同源转发、工具宿主工作台、真实trace和读错提示；新执行仍应沿这条传输，不是新建浏览器直连 | 09：传输继续只做Adapter，chat.send/job/trace关联新引用；对象结果定向刷新。已有真实reasoning继续读取，慢/断连不制造模型解释或另一套页面业务状态 |

### 10.2 目标目录：迁入同一业务包，旧根不另长一套

以下为**建议目标位置，尚未创建这些文件**。只列有明确职责的文件；先按完整用户切片迁实际Implementation，不先机械搬全仓。包入口只导出公共Interface，内部代码承接真实复杂性，不能全部原样转调旧模块。

```text

  harness/
    unified_service.py               # 保留受支持启动入口，调用装配
    fish_agent/
      bootstrap.py                   # 唯一装配及生命周期
      execution/                     # 主执行Module，一个公共Interface
        __init__.py
        runner.py                    # respond/_respond中的模型与工具循环
        wakes.py                     # 接收合并、45分钟/8条/3秒、认领与结束
        state.py                     # 同一Agent状态及已有运行记录适配
      experience/                    # 共同经历与上下文Module
        __init__.py
        events.py                    # 统一顺序、来源引用、覆盖与读取
        batches.py                   # 连续3秒文件批次
        context.py                   # 共同取材、预算和请求材料
        compaction.py                # DSH算法适配、检查点/目录/进度
      settings/                      # 运行设置Module
        __init__.py
        personas.py                  # 草稿/保存/活动版本
        models.py                    # Fish模型/凭据引用/用途
        expression.py                # 唯一长度/气泡/节奏来源
      tools/                         # 工具执行Module
        __init__.py
        registry.py                  # 现有工具登记/发现/执行
        effects.py                   # 发送、整理、续醒等真实效果交接
      knowledge/                     # 资源与记忆Module
        __init__.py
        memory.py                    # 统一读取、修订、来源、现有向量召回
        curation.py                  # 唯一完整整理程序/运行结果
        indexes.py                   # FTS5/目录及现有向量更新
        documents.py                 # 原件/资料块/分类（现有实现迁入）
        vision.py                    # 现有原图/局部/媒体能力
      plans/                         # 计划与履约Module
        __init__.py
        schedules.py                 # 计划/到期实例/履约，提交同一主执行
      delivery/                      # 投递Module
        __init__.py
        receipts.py                  # 气泡/原件/实际结果，调用通道Adapter
      adapters/
        qq.py                        # QQ接收/发送的真实传输
        desktop.py                   # 客户端输入/呈现的差异
        model.py                     # 复用ModelGateway的真实传输
        workbench.py                 # 现有动作到公共Interface
```

七个Module是公共业务职责，内部文件不是新的独立插件/进程。`fish_agent`只是现有Fish源码内的包，不是另一个Agent、运行根或数据库；M01仍延期。非本轮涉及的工具Implementation先在其归属明确后迁入，不为目录整齐搬动用户工程或下载资产。前端仍在既有ui-fish包；对象操作Interface置于原客户端对象职责附近，沿原workbench-client调用，不建新的全局状态大仓。

### 10.3 现有存储与最小扩展落点

| 位置 | 保留 | 拟扩展/替换 | 唯一写入者 |
|---|---|---|---|
| `data/qq/qq.db`、`data/qq/desktop-chat.db` | 原始消息/会话和用户已删除事实 | 原来源Adapter使用共同记录操作；原文更正/删除同步FTS；必要的来源代次/修订用于引用，不能从裸ID大小判跨库先后 | 输入/记录Adapter，业务不跨模块直接SQL |
| `data/social/experience.db` | experience_turns/considered、compactions、context_checkpoints、chat_catalog | 统一输入引用/接收序号、Agent运行状态及合并唤醒、文件批次及条目；experience_turns关联输入快照/结束事实。主执行状态和开始认领/清计数在同一短提交；压缩检查点/目录/进度在同一成功提交，停止另写原库活动chat_summaries | execution写运行/唤醒；experience写事件/批次/覆盖/压缩，各有私有写函数，不互相改字段 |
| `data/qq/memory.db` | memories/memory_history/exclusions/learning_jobs/memory_embeddings及既有小结 | 统一Agent读取与来源映射；适用的memories FTS5；一份整理运行记录关联范围/修订/步骤和结果。正文/修订/索引失效一起提交；向量仍沿既有工作执行，不复制正文库 | knowledge |
| `data/social/owner-control.db` | scheduled/occurrences及既有计划引用 | due只提交主执行并保留实例关联，结果由同一履约方法登记；手动运行和暂停状态由原计划职责处理 | plans |
| 原附件/资料登记及文件目录 | AttachmentStore、doc_store、表情原件与资源引用 | 由batch关联原资源；整理及索引沿原来源更新，不复制一份到新“批次附件库” | knowledge；experience仅存引用/批次 |
| `data/social/workbench.db`及现有trace存储 | 客户端request_id/job引用、真实模型字段与工具记录 | job映射统一提交/回合引用，退出它自己的模型执行队列；状态读主执行/整理/投递真实结果，不独立更新一份推测进度 | 工作台适配只维护请求映射；业务状态由相应Module |

这些是设计扩展，不是本轮执行的DDL；实施时在相关负责Module的既有迁移入口一次完成，不手工在生产库拼表。统一输入条目只保存原记录引用和必要排序/关联，不再复制整份历史。必须新增的状态分别解释用途：输入序号用于跨来源排序；合并唤醒用于一个消费过程；Agent状态用于计数/定时/连续次数；文件批次用于3秒归批；整理运行记录用于三个入口共用进度。既有表能表达的字段直接扩展，不为每个步骤造新表/数据库。

### 10.4 按调用者迁接与退役交接

1. **01/06首个完整过程：** 装配唯一主执行→QQ/客户端submit→统一接收/触发→原模型/工具循环迁入→实际投递结果回读。迁同一个调用者时退出其旧执行职责；不能让旧group pump、私聊worker、桌面executor与新runtime都处理该输入。提醒实例仍保留不同履约义务。
2. **08文件过程：** 原附件登记→连续3秒批次→同一主执行材料→原资源读取。退出各入口自建接收缓冲/单独准备同一附件的决定，保留下载/解析及真实外部格式限制。
3. **04记忆过程：** 先切统一知识Interface的真实调用者，包括ToolHost、主上下文、社交/资料工具、MemoryEngine、MemoryDigest、MemoryMaintenance和Workbench；再迁入共用整理程序。迁完相同职责即退出旧host.worker/QQ daily tick/独立maintenance的重复付费消费，不用新organize再包住三条旧循环。当前ToolHost未发现现成完整整理工具，拟在同一登记内补`fish_memory_curate`：start接受可选资料引用/时间范围，省略则增量；status接受operation_id读同一进度/结果。既有`memory.curate.run`改交同一organize并返回operation_id，新增对应`memory.curate.status`读进度；工具说明只写用途/参数/调用和结果，不附整理义务。它们是拟新增/迁接动作，不是已经上线的名称。
4. **01压缩过程：** 用DSH config/region/summarizer/检查点对应算法替换已有compaction内部Implementation；同一来源范围和版本发布后，停止原库chat_summaries活动读写。保留历史原件、辅助归纳能力及关闭设置，不保留两套自动触发。
5. **09前端过程：** 原chat.send/job、memory.curate.run、memory.search/save/merge等动作映射同一Interface，新的operation进度沿现有工作台查询；Tasks和ThoughtRail用真实引用定向刷新。普通保存不加模型调用；有用的现有按钮/后端方法继续用，不按旧“未实施”标签整页重写。
6. **退出与发布：** 更新启动入口、所有真实导入/调用者和实际发布清单；`tools/package_release.py`按source_files及归档清单校验，目录迁移不能继续把旧文件/旧入口留在发布包生效。必要临时迁移接法在原票列明退出条件，相关调用者迁完即退役；停用实体按既有需要清理方式处理，原始数据与无关工具不删除。本轮不执行打包/部署。

当前入口协议在相关完整切片接通时同时迁消费者，不长期新增同义版本路由。需要跨QQ进程提交时，在现有ToolHost本机传输增加主执行动作，复用认证/配置来源；它是submit的Adapter，不拥有另一套队列。主模型执行不得仍藏在旧适配器的回调中。

### 10.5 修改集中位置、依赖与必要核对

| 后续要改什么 | 主要修改位置 | 无需跟着修改的部分 |
|---|---|---|
| @/8条/45分钟、3秒续醒和计数时点 | execution/wakes和其状态操作；工具仅提交参数 | 群/私聊/桌面不再各改触发条件，记忆不负责调度 |
| 文件批次时长/交接 | experience/batches，必要对应输入/资源字段 | 不改自唤醒工具的3秒，不另改人格/模型参数 |
| 上下文预算/DSH压缩内部算法 | experience/context/compaction | 通道不重新拼历史，前端不算token或截断来源 |
| 记忆检索/整理程序 | knowledge/memory/curation/indexes | 每日、工具和按钮同用入口，不三处改逻辑 |
| 回复长度/动作/气泡节奏 | settings/expression和delivery各自职责 | 不改上下文预算，不给群/私聊新增两套长度规则 |
| 操作界面布局/对象编辑 | 原ui-fish对象操作及页面 | 主执行不依赖React，DSH宿主不接管业务规则 |

依赖分类继续沿权威规格4.10.5：本地SQLite/文件用实际临时根，QQ/模型用既有实际Adapter和必要替代返回；不为本表新建每表仓库层或测试插槽。必要核对跨公共Interface看接收顺序、计数时点、真实结果和保存后读取；旧测试若仅断言旧scope隔离、旧工作模式或私有调用顺序，随其Implementation退出，独立有效行为保留。

本轮只做源码阅读和设计，无模型/API请求、QQ实发、生产写库、代码变更或服务重载。to-tickets接本节和权威4.10.6分解原票，不再重复做一遍全仓审计；真正开始改代码前复核相关方法是否被并行工作更新即可。

<a id="ticket-code-map-20261025"></a>

## 11. 2026-10-25 to-tickets：按具体子票领取的源码与退出位置

本表是第10节设计的实施落点，不另立需求；准确行为与Interface仍见权威规格4.10.6。下列拟目标包均在现有Fish源码根内，未实际创建。领取时只复核该行真实方法的新改动，不重复审计全仓。

| 子票 | 当前应读/改的位置（相对Fish源码根，前端另注明） | 迁入的Implementation/调用者 | 退出与交接 |
|---|---|---|---|
| 01-01 | harness/unified_service.py:main；tool_host.py:serve/dispatch；workbench.py:chat.send/_send；desktop_chat.py:receive/send/_chat_turn；fish_chat.py:respond/_respond；chat_request.py；storage.py:thinking_slot | fish_agent/bootstrap、execution/runner/state及experience/context的首个完整过程；ToolHost与Workbench注入同一个runtime；原有效ModelGateway/工具循环/配置快照迁入 | 先接客户端submit/status及真实结果，退出桌面独立生成executor和入口自拼材料；启动/关闭与launch_fish.py/Fish.spec/实际发布导入同步，QQ暂未迁入的代码不得在生产与新runtime双跑 |
| 01-02 | qq_agent_loop.py:on_event/_worker/_process；owner_private.py:generate/process/reply/fault_notice；qq_store.py:record_message；tool_host.py的本机传输；bot_control.py:_deliver/deliver_bubbles | QQ输入Adapter→同一runtime.submit；experience/events统一来源/顺序；delivery及QQ发送Adapter迁有效私聊投递 | 退出私聊worker/generate主思考，保留真实来源和系统故障通知；原QQ/客户端请求映射同一执行引用，不另存推测成功 |
| 01-03 | qq_agent_loop.py:_on_group/_fish_start/_fish_reply；group_policy.py:observe及实际来源判断；experience.py；desktop_context.py | execution/wakes的@/8条判断、统一短交接/认领及计数；experience/events批次快照和人物/引用适配 | 退出每群pump的生成职责及按旧session分割消费；group_policy保留有效观察/既有通道设置，旧决定不再二次否决已定触发 |
| 06-01 | group_proactive.py:request_self_wake/_take_self_wake/claim/mark_sent/tick/evaluate；tool_host.py:fish_think_again；agent_scheduler.py旧自主默认初始化 | execution/wakes/state拥有45分钟截止、自唤醒请求/次数；工具执行effects提交狭义followup请求 | 退出随机/退避/fresh与删除任务即丢次数；不混入experience/batches的文件3秒；旧两小时默认初始化不得与新默认触发并行 |
| 06-02 | agent_scheduler.py:loop/_run；fish_schedules.py:detect/claim/run/_thought/run_now/pending/finish；bot_control.py:plans/execute；workbench.py:schedules.* | plans/schedules保留计划/实例及到期交接；主执行按实例处理，delivery回履约；现有任务页读同一实例 | 退出计划线程自行respond和分散发送；修暂停计划手动实例的pending/claim路径，不用临时恢复/暂停；后续09-02只做完整管理与结果呈现 |
| 08-01 | attachment_store.py:begin/append/finish/import_path/_register/read_range；multimodal.py:attachment_path/document_message/prepare_content；两个输入Adapter | experience/batches连续3秒窗口和引用；knowledge/documents/vision复用下载、原件、解析和读取；runtime.prepare按批接续 | 退出入口各自计批/重复准备和重复大小决定；原件不复制到新批次库，慢下载不延长归批；继承原08大附件能力范围 |
| 01-04 | context_budget.py；context_compaction.py:_trigger_tokens/auto_enqueue/tick/_record_checkpoint；desktop_context.py:retain_tokens/history；experience.py:requested；workbench.py上下文动作 | experience/context/compaction：当前模型/统一预算、DSH算法迁写、同一提交发布检查点/目录/进度 | 停止原库chat_summaries活动读写及8000/800/2000补丁；保留原文与辅助执行。DSH参考source/dsh/packages/compaction/compaction-basic/src/config.ts、region.ts、summarizer.ts及compaction/src/checkpoint.ts，遵MIT许可 |
| 04-01 | memory_store.py:_visible_scopes/get/save/delete/remember_social；memory_engine.py:recall；memory_evidence.py:save_from_tool；tool_host.py记忆/社交分支；memory_digest.py；desktop_context.py；workbench.py:memory.* | knowledge/memory统一Agent生产读视图、来源/修订/证据及实际向量调用；所有上述消费者切同一Interface | 退出按群/私聊硬隔离和对应工具文案；不把全部scope直接改成owner而丢来源，不恢复删除记录；现有工作台合并/停用/保存复用 |
| 04-02 | history_index.py:ensure/search；history_catalog.py:search_catalog/read_range；doc_store.py及MemoryStore/原记录负责写方法；tool_host.py历史/资料工具 | knowledge/indexes与experience/history读取：官方FTS5、同源增删改、目录/概述/原文和资源关联 | 保留已接通FTS工具与桌面字段适配；替换MAX追加不更正、错误来源与截断候选，不复制另一套历史/记忆正文 |
| 04-03 | memory_maintenance.py:HistoryCursor/MemoryMaintenance.run/_daily/_weekly/start_background；daily_curate.py:run/tick/_cleanup/_emoji；memory_store.py:enqueue/learn_one；tool_host.py:worker/工具登记；qq_agent_loop.py:_maintenance；workbench.py:memory.curate.run | knowledge/curation的唯一organize/operation；同一学习/来源/小结/表情步骤；拟fish_memory_curate start/status、memory.curate.run/status；plans提供每日启动 | 同范围/修订共用执行引用；迁完对应职责后退出host.worker/QQ daily tick/独立maintenance的重复学习和定时，不用一层organize包三条旧循环；前端09-06消费此结果 |
| 07-01 | turn_trace.py；experience.py的时间/结果；governance.py既有故障语义；workbench.py:chat.thoughts/chat.reasoning/chat.job/对象详情；前端FishChatPage.tsx:ThoughtRail及现有刷新 | 统一提交/回合/任务/资源/整理/投递关联；主执行status和原trace呈现，不引入第二份页面状态 | 退出裸消息号误绑定和推测成功/沉默；原模型字段、五时间点和真实中文错误保留，给09-10一次总体验入口 |

### 11.1 既有09子票本轮新增的真实前置

| 09子票 | 本轮新增前置 | 原因/实现交接 |
|---|---|---|
| 09-02 | 06-02 | 暂停计划立即运行及到期实例必须经同一主执行履约；前端已有按钮和run_now不能当作该执行含义已完成 |
| 09-06 | 04-03、01-04 | 整理工具/按钮/每日共用进度，统一检索/修订和压缩管理都要有真实后端Interface |
| 09-07 | 04-02 | 完整试查/概述/原件沿统一索引与来源读取，不在页面重建查询 |
| 09-08 | 01-01 | 工作区交文件办事及取消/成果接新主执行引用；保留已有AgentTools，退出旧工作模式隐式依赖 |
| 09-10 | 07-01（另有原09-02—09-09） | 新后端所有运行事实接通后，与新工作台在同一次私聊/客户端过程集中验收 |

09-01/03/04/05/09没有增加无关后端前置；原09-01的工作台入口依赖继续保留。完整原09职责和具体页面源码仍按第8/10节，不复制成第二组前端票。原父项和历史状态保持，本轮真正领取新子票。
