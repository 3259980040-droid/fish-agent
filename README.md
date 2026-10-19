# dsh-personal

> 2026-10-18 四轮开发第 1 轮入口：`dist-round1\Fish\Fish.exe`（完整目录运行）；开发入口 `python -m fish_desktop.app`。旧 `dist\Fish` 与 `dist-codex\Fish` 保留。
>
> 新版普通聊天默认接收照片，增加主人账号权限、认真执行的人格规则、连接设置和本地发送/工具拦截。12 项目标与四轮安排见 `FISH-TASKS.md`，各轮实际结果与剩余项见 `FISH-ROUNDS.md`。更早的接手记录为 `TAKEOVER-2026-10-18.md`。
> 旧 `harness/app.py` 保留为 legacy/debug/recovery console。

研究/知识库 DeepSeek 与 QQ 群 DeepSeek 的双实例脚手架。
完整设计见 [ARCHITECTURE.md](ARCHITECTURE.md)；
产品构想（客户端 / 两库 / 安全闸门 / 插件）见 [PRODUCT.md](PRODUCT.md)。

## 先看结构

```
dsh-personal\
├── config.ps1              # 路径与端口，改这一处即可
├── homes\research\         # 研究实例的 DSH_HOME
├── homes\qq\               # QQ 实例的 DSH_HOME
├── knowledge\              # 研究实例工作目录 = 知识库
│   ├── AGENTS.md           # 研究实例操作手册
│   ├── notes\{video,repo,topic,note}\
│   └── profile\{preferences.md,signals.jsonl}
├── qq\                     # 人格/治理源文件 + OneBot 配置
├── qq-work\                # QQ 实例工作目录
│   └── AGENTS.md           # 群聊人格与边界
├── shared\digest\          # 单向出口：研究写，QQ 只读
├── dashboard\              # 本地监控面板（双击看两个实例）
├── tools\kb.py             # 知识库 CLI（零依赖）
└── scripts\                # 启动脚本
```

## Phase 0：把两个实例跑起来

```powershell
# 研究实例
$env:DSH_HOME = "F:\Downloads\dsh-personal\homes\research"
dsh web --port 3080 --no-open

# QQ 实例（另开一个终端）
$env:DSH_HOME = "F:\Downloads\dsh-personal\homes\qq"
dsh web --port 3090 --no-open
```

第一次会初始化各自的 profile。两个 home 完全独立：各自的 `.credentials.yaml`、session、插件。

## 看板：Mission Control

```powershell
python dashboard\server.py --port 3099
```

打开 <http://127.0.0.1:3099>，演示数据加 `?demo=1`。
它探活 3080/3090、读知识库索引、日报和 `qq-work/state.json`，一页看完两个实例。
数据源与状态格式见 [dashboard/README.md](dashboard/README.md)。

## Phase 1：研究实例装 B 站数据层

```powershell
$env:DSH_HOME = "...\homes\research"
dsh plugin --profile web add github:c1overzz/dsh-bili-scroll
```

启动后进 `:3080` 的设置页「刷B站」：装依赖 → 扫码登录 B 站。
它负责取推荐、字幕、评论；我们的 `kb.py` 负责把结果写进自己的知识库。

GitHub 侧不需要插件，装好 `gh` 并登录即可：

```powershell
gh auth login
gh search repos "topic:llm" --sort stars --limit 20
```

## Phase 2：知识库

```powershell
python tools\kb.py stats
python tools\kb.py add --type video --title "示例" --url "https://..." --tags llm,agent --rating 4 --summary "一句话"
python tools\kb.py search "agent"
python tools\kb.py signal --id <笔记id> --kind like --reason "有代码，能直接用"
python tools\kb.py digest
```

`knowledge\AGENTS.md` 已经就位，研究实例启动时会自动加载（DSH 的 agent-instructions 会读 cwd 下的 AGENTS.md），按模板写笔记。

## Phase 2.5：先定她的人设（不动代码）

看 [qq/PERSONA-FRAMEWORK.md](qq/PERSONA-FRAMEWORK.md)，把五件事定下来：名字、是否承认 AI、第一版是否只 @ 回、进几个群、每天最多几句。
然后复制 `qq/persona.example.yml` → `qq/persona.yml`，`qq/policy.example.yml` → `qq/policy.yml`，按答案改。
第一版把人格手写进 `qq-work/AGENTS.md` 即可；治理参数先靠规则约束，v1 再交给 `qq-gate`。

## Phase 3：QQ 实例

1. 单独装 NapCat，用你的专用 QQ 号扫码登录。
2. NapCat 里开 OneBot 11 正向 WebSocket：`127.0.0.1:3001`，设 token。
3. 安装适配器：

```powershell
$env:DSH_HOME = "...\homes\qq"
dsh plugin --profile web add github:Hoshino-Yumetsuki/dsh-onebot
```

4. 把 `qq\onebot.patch.yml` 的配置合并进 `homes\qq\profiles\web\cordis.patch.yml`，改成你的群号、QQ 号，token 用 `dsh` 的 credential `ONEBOT_ACCESS_TOKEN`。
5. `qq-work\AGENTS.md` 已经就位，按你的群改一下语气和名字。
6. 重启 QQ 实例，在群里 @ 它测试。

## Phase 4：加固

- 给 QQ 实例单独建 Windows 本地用户，`icacls` 拒绝其读取 `knowledge`。
- 两个实例分别做计划任务/服务，开机自启，崩溃重启。
- `knowledge` 做成 git 仓库，每天备份；`homes` 和 cookie 不要进 git。

## 注意

- 官方 `dsh-qqbot` 走 q.qq.com 的 bot，**不能**用普通 QQ 号登录；你这种情况要用 NapCat + `dsh-onebot`。
- 个人号机器人有掉号风险，控频、别群发。
- B 站 cookie 会过期，过期重扫。
