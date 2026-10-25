# Fish 自有工具与执行边界

所有定义只从 ToolHost `/catalog` 取得；内置工具与已启用 MCP 共用同一注册、身份、执行入口。正常聊天不启动 DSH 模型、不载入 DSH AGENTS 提示词。当前生产目录共118项，含实际发现的63项Tulpa QQ MCP；这是本次环境观测值，不能写成永久固定数量。

| 模块 | 唯一责任与接口 | 实际实现 |
|---|---|---|
| 输入和共同经历 | 原消息、独立引用/人物/场所、实际自身投递；在取得主思考槽后组装 | desktop_context、experience；原QQ/desktop-chat数据库不被另抄一份替代 |
| 模型和预算 | Fish API、运行时窗口、逐层token裁剪及实际用量 | model_gateway、context_budget；30%为当前容量上限，不要求填满 |
| 工具宿主 | 目录注册、主人身份及实际调用 | tool_host；agent_tools提供文件、原图、PowerShell进程和QQ接口 |
| 浏览器/桌面 | Fish独立浏览器资料目录；真实屏幕输入 | interaction_tools，Playwright Edge及Windows原生接口；任意应用动作仍须看真实结果 |
| 可选子任务 | Fish自己的API/人格/工具，任务结果、文件与取消 | task_delegation；父主思考继续，子任务不经DSH注入；已主动送达的结果不再由QQ结果监视器重复补发 |
| 记忆与资料 | 来源、词法+有效向量、原件/读取范围、日周月整理 | memory_store/memory_engine、doc_store；fish_library仅适配接口，语义不可用明确显示词法 |
| 视觉 | 同一主模型看原图/局部/帧，不默认另起识图判断 | image_views，multimodal；本机图片可由fish_file_read直接返回像素 |
| 表达与投递 | 既有气泡规则、唯一通道分段与实际回执 | chat_output、owner_private/BotControl；不把工具staged当送达 |
| 主执行 | 唯一消费者、唯一模型循环、输入合并与唤醒判定 | fish_agent（execution/experience/settings/tools/delivery/plans/adapters）；客户端、QQ私聊、群、计划、时钟都经同一回合 |
| 唤醒与批次 | 客户端/私聊立即、群@或8条、45分钟、3秒续醒；文件固定3秒归批 | execution/wakes、experience/batches；批次只供材料，不另起思考 |
| 定时 | reminder可靠履约，autonomy可不行动；实例只提交主执行 | plans 经主执行，fish_schedules 保留计划/实例与回执；已发送或未知结果不自动补发 |
| 记忆整理与检索 | 每日/工具/客户端共用一条执行记录；索引→概述→原文分层回查 | memory_curation、knowledge、history_index/history_catalog、memory_store；FTS5真实调用，语义未就绪明确降级 |
| 客户端状态 | 当前真实阶段、服务商reasoning副本、工具与投递、批次与计划实例 | turn_trace + turn.view→workbench→ui-fish；不再为解释自身状态调用另一个模型 |

`agent_tools.py` 文件/进程接口参考本地DSH的fs-local、tool-fs和tool-bash，用Python实现适配，并非宣称逐字复制TS执行器全部特性。主人明确指定已有工程时使用其真实路径；默认文件在workspace分类目录。源码仍在E盘原根，M01没有执行。

QQ操作来自当前Fish账号的真实OneBot/Tulpa接口，不能冒充主人的QQ身份。MCP采用原版授权登记的完整能力；某个接口服务端不支持、认证失效或回执不全均如实返回。声音、视频和跨设备操作仍是接口预留或现有外部依赖，不能据此宣称已完整验收。

本机语义模型使用vendor/runtime/memory-python独立环境，依赖见requirements-memory.txt；模型BAAI/bge-small-zh-v1.5。浏览器使用Fish自己的workspace/browser/profile。安装依赖、模型缓存、运行数据和登录凭据不进入GitHub发布包。

## 工具执行入口的已知例外

`fish_emoji_search` / `fish_emoji_read` 的**定义**仍只来自 ToolHost `/catalog`，但主执行循环在拿到结果后要为模型附加真实预览图，所以这两个工具在 `fish_agent/execution/runner.py` 内直接调用 `emoji_store`（`EMOJI_TOOLS` 常量与分支）。这是本批保留的既有例外，不是第二套工具注册或第二套身份；其余内置工具与 MCP 一律经 ToolHost `/execute`。清理方向是把预览作为工具结果的受控图像附件回到同一执行入口，已记录为后续工程债。
