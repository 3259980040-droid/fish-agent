# Fish Harness

一个**本地桌面程序**，不是网页。用来管你在本机跑的多个 DeepSeek Agent。

## 跑起来

```powershell
cd F:\Downloads\dsh-personal
python harness\app.py
```

自检（不开窗）：

```powershell
python harness\app.py --selftest
```

## 它能做什么

| 能力 | 说明 |
|---|---|
| 多 Agent | 左侧列出研究实例 / 大肥鱼，单独启动、停止、全部启动 |
| 启动方式 | 用 `DSH_HOME=<agent.home>` 起 `dsh web --port <port>`，工作目录是 `agent.workspace` |
| 日志 | 每个 Agent 的 stdout 实时进「总览」的日志框 |
| 全局注入 | 「全局注入」页写一段规则，一键写进每个 Agent 的 `$DSH_HOME/AGENTS.md`，并自动备份旧文件 |
| 强行切断 | 右上角红按钮，确认后 `taskkill /F /T` 杀掉所有受管进程树 |
| 本地账本 | SQLite `harness.db`：agents / runs / events / costs / docs |
| 事件流 | 启动、退出、注入、切断都记一条 |

## 强行切断的边界

v0 的「切断」是**进程级**：

- 杀掉 Fish Harness 启动的 `dsh` 进程，以及它拉起的子进程（`/T`）。
- 正在跑的对话、工具调用会一起中断，未落盘的内容可能丢。

还没有做的：

- **Job Object**：把每个 Agent 放进 Windows Job，`KILL_ON_JOB_CLOSE`，连脱离进程树的孙子进程也能收掉。
- **全局热键**：不切窗口也能切（`RegisterHotKey`，比如 `Ctrl+Alt+Shift+K`）。
- **只停当前一轮**：DSH 的 turn 级 abort；v0 只能整进程停。

## 全局注入怎么生效

```
harness/global/AGENTS.md   ← 你在这页编辑
        +
harness/agents/<id>.md     ← 每个 Agent 的增量规则
        ↓ 注入
<agent.home>/AGENTS.md     ← DSH 新会话读取
```

- 优先级：全局 < Agent 增量 < 工作目录里的 `AGENTS.md`。
- 只影响**新会话**；已经在跑的实例要重启。
- 旧文件会备份成 `AGENTS.md.bak`。

## 配置

`harness/agents.json`：

```json
{
  "dsh": { "command": "npx", "args": ["-y", "@deepseek-ai/dsh"] },
  "agents": [
    { "id": "research", "name": "研究实例", "home": "homes/research",
      "workspace": "knowledge", "port": 3080, "model": "deepseek-flash" }
  ]
}
```

- 路径相对 `dsh-personal/`，也可以写绝对路径。
- 如果 `dsh` 已在 PATH，把 `command` 改成 `dsh`，`args` 改成 `[]`，启动会快很多。
- Agent 要自定义参数时加 `"args": ["web", "--port", "3080", "--no-open"]`。

## 数据库

`harness/harness.db`（SQLite，纯本地）：

| 表 | 用途 |
|---|---|
| `agents` | id / 名字 / home / workspace / port / model |
| `runs` | 每次启动的 pid、开始/结束时间、退出码、原因 |
| `events` | 启动、退出、注入、切断 |
| `costs` | 时间、Agent、人民币、token 数、备注 |
| `docs` | 每次注入了哪个文件、多少字符 |

看数据：程序左下角「打开数据库」，或 `sqlite3 harness.db`。

## v0 没做的

- 不做 Agent 循环本身：复用 DSH，Harness 只做「壳 + 监管 + 注入 + 切断」。
- 不做网页：没有 HTTP 端口，没有浏览器。
- 不做权限系统：谁打开这个程序，谁就能切。
- 不做自动重启：进程退出只记录，不拉起（避免切断后被自动救活）。
