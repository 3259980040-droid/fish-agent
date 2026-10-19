# Fish Harness · 产品构想 v1

> 你的许愿：一个客户端调设置；两个 DeepSeek 各用各的库；QQ 的会存表情包、会回消息；
> 危险输出本地拦；插件可配。
>
> 这份文档把愿望拆成模块，并标出哪些已经有了、哪些是下一步。

---

## 0. 一句话

**Fish Harness = 本地控制面 + 两个互不打扰的 Agent + 出门前的安全闸门 + 可插拔的插件运行时。**

---

## 1. 愿望 → 模块

| 你的愿望 | 对应模块 | 现状 |
|---|---|---|
| 客户端调设置 | 控制面 Settings | v0 有雏形（Tkinter），设置页待补 |
| 人物性格 | Persona | v0 有 `persona.yml` + AGENTS.md |
| Agent 注入 | Injection | v0 已实现（全局 + 单 Agent → `$DSH_HOME/AGENTS.md`） |
| API 接入 | Providers | 待做（每 Agent 独立 key / baseURL） |
| B 站 DeepSeek | Research Agent | 架构已定，未接 B 站数据层 |
| QQ DeepSeek | QQ Agent | 架构已定，未装 NapCat |
| 两边不同数据库 | Data Boundary | 待做（现在只有 harness.db） |
| QQ 存表情包 | Meme Vault | 待做 |
| QQ 回消息 | OneBot Reply | 现成（dsh-onebot） |
| 危险言论本地拦 | Guard | **v0 已有规则引擎 + 客户端「安全」页** |
| 多插件（视频理解） | Plugin Runtime | 待做（manifest 已设计） |

---

## 2. 总体架构

```
┌──────────────────────────── Fish Harness（桌面客户端）────────────────────────────┐
│                                                                                  │
│  控制面                     监管                      安全闸门                    │
│  ├ 人格 / 注入              ├ 启停 / 日志             ├ 入站检测                  │
│  ├ 模型 / API               ├ 强行切断（进程树）       ├ 出站检测                  │
│  ├ 插件 / 配额              └ Job Object（v1）         └ 动作检测（工具/域名）      │
│  └ 数据目录                                                                      │
│                                                                                  │
│  插件运行时                                                                       │
│  ├ MCP server（视频理解、B 站、GitHub…）                                          │
│  ├ skill（prompt 包）                                                             │
│  └ panel（客户端页签）                                                             │
│                                                                                  │
│  本地存储                                                                         │
│  ├ data/research/research.db     研究：笔记、来源、偏好、花费                       │
│  ├ data/qq/qq.db                 QQ：消息、表情包、人物卡、配额、审核日志           │
│  └ data/shared/digest/           单向：研究写，QQ 只读                             │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 两个 Agent，两套数据

| 资源 | 研究实例 | QQ 实例 |
|---|---|---|
| DSH_HOME | `homes/research` | `homes/qq` |
| 工作目录 | `knowledge/` | `qq-work/` |
| 数据库 | `data/research/research.db` | `data/qq/qq.db` |
| API key | key A | key B |
| 插件 | B 站、GitHub、视频理解 | OneBot、表情包 |
| 网络 | 可访问 B 站 / GitHub | 只访问 B 站只读 + OneBot 回环 |
| 输出 | 笔记、日报 | 群消息、表情包 |

**硬规则**：

1. 两个库物理分开，连接串不同，备份策略不同。
2. QQ 实例读不到 `knowledge/`，研究实例读不到 `qq.db`。
3. 唯一通道是 `data/shared/digest/`，单向。
4. 两把 API key；一把被限流不影响另一把。

为什么分库而不是分表：两个 Agent 的数据寿命、隐私级别、清理策略完全不同。
群聊消息 30 天就该清，知识笔记要留几年；混在一个库里，备份和删除都会互相牵连。

---

## 4. 客户端设置

每个 Agent 一组页签：

| 页签 | 内容 |
|---|---|
| 人格 | 名字、自称、语气、口癖、示例对话、承认 AI 与否 |
| 注入 | 全局规则 + 该 Agent 的增量规则，保存后注入 `$DSH_HOME/AGENTS.md` |
| 模型 | provider、model、baseURL、key 引用、温度、最大输出 |
| 插件 | 该 Agent 启用了哪些插件，各自的配置 |
| 安全 | 规则集、动作（放行/改写/拦截/切断）、误杀复核 |
| 配额 | 每天多少轮、多少条、多少钱，超限行为 |
| 数据 | 库路径、备份、清理策略 |

全局设置：开机自启、热键、主题、日志级别、默认模型。

**密钥**：不写明文。Windows 用 DPAPI 加密后存 `harness/secrets/<id>.bin`；
配置里只存引用名。没有 DPAPI 的机器退化为「环境变量引用」。

---

## 5. QQ Agent 的能力

### 5.1 回复

`onebot_reply({text, images, quote})`，一轮一次，idle 后发。已有。

### 5.2 表情包仓库（Meme Vault）

```
群消息图片
  → 下载到 data/qq/memes/<sha256>.<ext>
  → qq.db 记：hash、来源、发送者、时间、标签、使用次数
  → 工具：meme_search(tag/关键词)、meme_send(hash)
```

- 去重靠 sha256；同一张图只存一次。
- 标签由 Agent 提议、你确认；不自动给敏感图打标签。
- 发送频率受媒体配额约束（每小时 2、每天 6）。
- 只存群聊公开图片；不存私聊、不存带人脸/隐私的图（可加本地人脸检测，v2）。

### 5.3 记忆

- 短期：最近 30 条（2 小时）。
- 群档案：`data/qq/groups/<群号>.md`，周更。
- 人物卡：`data/qq/members/<id>.md`，只记公开说过的东西。
- 跨群隔离。

### 5.4 配额

四层：全局日 / 单群小时 / 单群冷却 / 单用户连续轮次。超限默认沉默。

---

## 6. 安全闸门

### 6.1 三条流水线

```
入站消息 ──▶ 入站检测 ──▶ Agent ──▶ 出站检测 ──▶ 发送
                                      │
工具调用 ──▶ 动作检测 ────────────────┘
```

| 闸门 | 查什么 | 命中后 |
|---|---|---|
| 入站 | 提示注入、要求生成攻击/仇恨内容 | 不启动 Agent，回一句拒绝 |
| 出站 | Agent 准备发出去的话 | 不发送，替换 fallback 或沉默 |
| 动作 | 工具要访问的域名/命令/文件 | 拒绝执行，记录 |

**重点在出站**：讨论一个话题 ≠ 生成仇恨内容。入站只拦「要求生成」，出站才拦「已经生成」。

### 6.2 判定与动作

| 动作 | 含义 |
|---|---|
| `allow` | 放行 |
| `rewrite` | 放行但要求改写（比如去掉人身攻击） |
| `block` | 不发送，用 fallback 或沉默 |
| `panic` | 不发送 + 通知监管进程切断当前 Agent |

规则来源：

1. `harness/safety/rules.json` — 结构性规则（攻击、自伤、违法、提示注入）。
2. `harness/safety/local_terms.txt` — 本地敏感词，一行一个，不进 git。
3. 本地小模型裁决（v1）— 规则抓不到的改写/阴阳怪气，用小模型二分类。
4. 你的人工复核队列 — 被拦的话进 `qq.db` 的 `review` 表，你决定放行还是拉黑。

### 6.3 怎么做到「强行打断」

两种接法：

- **进程内**：DSH 插件监听 `tools/pre-execute`，`onebot_reply` 前调用 guard；命中就返回错误，回复不发出。
  优点：简单；缺点：和 Agent 同进程，Agent 被攻破时闸门也可能被绕过。
- **进程外（推荐）**：Agent 不直接发 QQ，而是把候选回复写到本地队列；
  Fish Harness 的 guard 进程检查通过后，再调 NapCat 的 `send_group_msg`。
  命中就丢弃，并可 `taskkill` 掉 Agent。
  优点：闸门独立，能真正「切断」；缺点：多一个进程和一条队列。

v0 先做进程内规则版；v1 换成进程外。

### 6.4 误杀

- 每条拦截都进 `review` 表，附原文、命中规则、时间。
- 同一规则一周误杀超过 N 次 → 自动降级为 `rewrite` 并提醒你。
- `local_terms.txt` 支持注释和分级：`critical|term`、`warn|term`。

---

## 7. 插件系统

### 7.1 插件类型

| 类型 | 例子 | 接法 |
|---|---|---|
| `mcp` | 视频理解、B 站、GitHub | MCP server，stdio |
| `tool` | 自定义 Python 函数 | DSH 工具 |
| `skill` | prompt 包 / 工作流 | 注入到 Agent |
| `panel` | 客户端页签 | Tkinter/WebView |

### 7.2 Manifest

见 [harness/plugins/examples/video-understanding.json](harness/plugins/examples/video-understanding.json)：

```json
{
  "id": "video-understanding",
  "type": "mcp",
  "agents": ["research"],
  "entry": { "command": "python", "args": ["server.py"], "cwd": "." },
  "permissions": { "filesystem": ["data/"], "network": false, "shell": false }
}
```

### 7.3 规则

- 插件只对 `agents` 里列出的 Agent 生效；QQ 实例默认拿不到视频插件。
- 权限最小化：要文件就只给 `data/`，要网就给域名白名单。
- 装插件 = 复制到 `harness/plugins/<id>/` + 校验 manifest + 重启对应 Agent。
- 插件崩溃不拖垮客户端：子进程 + 超时 + 重启上限。

---

## 8. 数据库

### 8.1 `data/research/research.db`

| 表 | 内容 |
|---|---|
| `notes` | 笔记元数据（正文仍在 Markdown） |
| `sources` | 视频/仓库来源、抓取时间、字幕状态 |
| `preferences` | 偏好信号 |
| `costs` | 花费 |

### 8.2 `data/qq/qq.db`

| 表 | 内容 |
|---|---|
| `messages` | 群消息（30 天滚动清理） |
| `memes` | 表情包 hash、标签、使用次数 |
| `members` | 人物卡 |
| `groups` | 群档案 |
| `budget` | 每日轮数/条数/花费 |
| `review` | 被拦回复的复核队列 |
| `incidents` | 严重事件（panic、连续违规） |

### 8.3 为什么分开

- 隐私：群聊里有别人的话，不能和研究笔记一起备份到网盘。
- 寿命：消息 30 天，笔记永久。
- 故障：QQ 库写坏了不影响知识库。
- 迁移：换机器时可以只搬 research，不搬群聊。

---

## 9. 分阶段

| 版本 | 内容 | 验收 |
|---|---|---|
| **v0（现在）** | 客户端壳、启停、日志、全局注入、进程树切断、guard 规则引擎 + 安全页 | 能开窗、能检测一句话、能杀进程 |
| v0.2 | guard 接进发送路径（进程内），拦截进 review 表 | 群里让它说危险话，发不出去 |
| v0.3 | 两个独立库 + QQ 表情包仓库 | 表情包能存能发；研究库不受影响 |
| v0.4 | 插件 manifest + 视频理解插件 | 研究实例多一个 `video_summarize` 工具 |
| v1 | guard 进程外 + Job Object + 全局热键 | 闸门独立，热键一按全停 |
| v1.5 | 本地小模型裁决 + 误杀降级 | 规则抓不到的改写也能拦 |

---

## 10. 要你拍板的四件事

1. **安全闸门的严格度**：宁可误杀（漏说一句）还是宁可漏放（说错一句）？
   建议：出站宁可误杀，入站宁可放宽。
2. **本地裁决模型**：只上规则，还是装一个 0.5B～1.5B 的本地小模型做二分类？
3. **表情包来源**：只存群里发过的，还是允许它自己上网找？
   建议先只存群里的，版权和风控都简单。
4. **插件隔离**：插件跑在同一个 Python 进程，还是每个插件一个子进程？
   建议子进程，崩了不拖垮主程序。
