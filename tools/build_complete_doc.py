from pathlib import Path

ROOT = Path(r"F:\Downloads\dsh-personal")

HEADER = r"""# Fish Harness · 完整文档包

> 版本：2026-10-17  
> 状态：**Phase 1A 已完成并冻结**；**Phase 1B Completed**；**Phase 2 最小闭环 Completed**（离线 18/18 + 真实「大肥鱼观看」3/3）；**Phase 3A Completed**（离线 + Fake NapCat 15/15）；**Phase 3B Transport 主链 Completed**（真实 QQ `1417334937`）；**Phase 3C Completed**（owner 私聊 reactive agent loop）；**Phase 4A Completed**（PySide6 Product Shell）；**Phase 4B Completed**（Windows onedir 包 + 真实按钮验收 + 生命周期）；**Phase 4C Completed**（Fish Design System / 六页 UI 改版）。  
> 用途：把当前有效的计划、规范、现状、人格、部署与参考分析整合成一份，供交接、评审、存档。  
> 权威顺序：**本文件（FISH-HARNESS-COMPLETE.md）是当前 master**；`Handoff v2`、Part 7 历史材料只作历史来源，低于本文件。  
> 注意：Part 7 是历史/辅助材料，不再作为实现依据。

## 目录

| Part | 内容 |
|---|---|
| Part 1 | Phase 1A / 1B 实施计划（含 Phase 1A 验收） |
| Part 2 | fish-qq Channel Adapter 接口、生命周期、错误/重连规范 |
| Part 3 | Phase 0 现状核对（版本、代码、数据、缺口） |
| Part 4 | 大肥鱼人格卡、治理参数、运行时人格 |
| Part 5 | NapCat 部署现状与启动器 |
| Part 6 | 参考项目拆解（QQ / B 站） |
| Part 7 | 历史与辅助材料说明 |
| Part 8 | Phase 1B 验收报告 |
| Part 9 | Phase 2 验收报告 |
| Part 10 | Phase 3A 验收报告 |
| Part 11 | Phase 3B 准备/验收报告 |
| Part 12 | Phase 3C Private Reactive Agent Loop |
| Part 13 | Phase 4A Fish Desktop Product Shell |
| Part 14 | Phase 4B Desktop Productization |
| Part 15 | Phase 4C Fish Desktop UI/UX Redesign |

---

## 0. 一页总览

### 0.1 一句话

Fish Harness 是一个**本地桌面多 Agent 宿主**：客户端先独立运行，DSH 只是 RuntimeAdapter 的当前实现；QQ 通道是自写的 `fish-qq`（Python），NapCat 只是 OneBot 传输层；B 站是 Source Adapter，不绑定 Runtime。

### 0.2 当前状态

| 模块 | 状态 |
|---|---|
| Fish Core 客户端（Phase 1A） | ✅ 已实现，17/17 核心检查 + 3 个 GUI 场景通过 |
| DeepSeekHarnessAdapter（Phase 1B） | ✅ Completed：真实模型、production web path、临时 Session Cancel、P0-5/P0-7 |
| fish-qq（Phase 3A） | ✅ Completed：离线实现 + Fake NapCat 15/15 |
| fish-qq（Phase 3B） | 🟢 Transport 主链 Completed：真实 QQ/私聊/群/reply/image/reconnect；2 个 allowlist 边界待样本 |
| QQ Agent loop（Phase 3C） | ✅ Completed：owner 私聊 → QQ Runtime，连续 6 轮、Cancel、Runtime 失败恢复 |
| Bilibili Source Adapter（Phase 2） | ✅ Completed：离线 18/18；真实「大肥鱼观看」3/3 |
| QQ Agent 人格/治理 | ✅ Completed：Phase 3C 真实 QQ 私聊 + canonical persona |
| NapCat | ✅ bot `1417334937` 已登录；OneBot `127.0.0.1:3001` 已配置 |
| 安全闸门 | 规则引擎可运行，但有误杀；**未接发送路径**，V0 不做 Content Filter |
| 本地视频理解 | `mcp-video-analyzer@0.11.0` 已确认接口 |
| 双业务库 | ✅ `research.db` / `qq.db` 已接 Phase 2 / Phase 3C |
| Fish Desktop（Phase 4A） | ✅ PySide6 Product Shell：六页 + process supervisor |
| Fish Desktop（Phase 4B） | ✅ Windows onedir `dist/Fish/Fish.exe`；真实按钮 17/17；Job Object 生命周期 |
| Fish Desktop（Phase 4C） | ✅ Fish Design System；中文六页改版；`大鱼 Fish`；packaged UI |

### 0.3 架构

```text
Fish Core（桌面客户端）
├─ RuntimeAdapter ── DSH（固定版本；Phase 1B）
├─ ChannelAdapter ── fish-qq（Python，WS 3101）
│                     └─ NapCat（OneBot 11，WS 3001）── QQ
├─ SourceAdapter ── Bilibili（收藏夹/字幕/视频信息）
├─ Domain ── research.db / qq.db
└─ Control ── harness.db（启停/事件/账本/文档）
```

两个 Agent：

- **Research Agent**：只读用户指定 B 站收藏夹，理解、汇总、记忆；V0 不自主刷全站。
- **QQ Agent（大肥鱼）**：主人私聊 + 一个指定群；有 canonical persona、连续 conversation 和聊天历史；**Long-term Memory 尚未实现**；V0 不做 Content Filter。

### 0.4 已冻结的决策

| # | 决策 |
|---|---|
| D1 | QQ 走个人号：NapCat + OneBot 11；不用官方 `dsh-qqbot` |
| D2 | Research / QQ 两个独立 DSH_HOME、进程、API key、数据库 |
| D3 | 人格名「大肥鱼」；被问是不是 AI 就承认，不装人 |
| D4 | QQ 活跃模式：全量读，默认不回；相关性与活跃度在 Fish Core |
| D5 | 监控报告达到约 ¥5 后停止新的模型任务；不设 ¥4.5 硬停线，将来如需 soft threshold 再单独配置 |
| D6 | 只进一个群；群号待定 |
| D7 | 主人 = QQ `<owner-qq>`；只认号；主人否决优先 |
| D8 | **仅约束 QQ 大肥鱼 Agent**：不写代码、不跑命令、不装软件、不改文件；Research Agent 不受此限 |
| D9 | 普通聊天允许响应式搜索/发送图片；**B 站视频分享**只发链接+封面，限频 |
| D10 | `research.db` / `qq.db` / `harness.db` 三库分责 |
| D11 | 客户端是桌面程序，不是网站 |
| D12 | Fish 保存 canonical persona/instructions；`$DSH_HOME/AGENTS.md` 只是 **DSH Adapter 的投影目标**；全局 < Agent < workspace |
| D13 | 安全闸门 V0 不接发送路径；只保留实验页 |
| D14 | 插件按 Agent 绑定、最小权限 |
| D15 | **Fish Core 独立于 DSH**：DSH 缺失/配置错误/启动失败都不能让客户端退出 |
| D16 | **QQ 通道自写 `fish-qq`**；`dsh-onebot-qq` 只作参考，不作运行依赖 |
| D17 | 先做 Phase 1A，验收通过后再做 Phase 1B；不开始 Phase 3 |
| D18 | `DSH_HOME` / port / profile / patch 属于 Runtime 配置，不是 Agent 字段 |

### 0.5 Phase 1A 验收摘要

核心检查（`python harness/phase1a_check.py`）：**17/17 PASS**。

| 场景 | 结果 |
|---|---|
| 无 runtime：配置可加载、Agent/Channel 仍在 | PASS |
| 无 runtime：状态 unconfigured | PASS |
| 无 runtime：三个数据库存在且可访问 | PASS |
| 无 runtime：人格可写可读、Agent 设置可保存 | PASS |
| cli 为空 / cli 不存在 / home 缺失 | unconfigured / unavailable / unavailable |
| 版本匹配但启动失败 | failed，不抛异常 |
| Channel 配置可读、可启动、可停止 | PASS |
| `agents.json` 损坏 | 不抛异常，记录 error，客户端仍可加载 |

GUI 自检：

| 场景 | 结果 |
|---|---|
| 正常配置（DSH 已配置） | 客户端启动；research/qq = stopped；channel = unconfigured；三库存在 |
| 完全没有 runtime 块 | 客户端启动；两个 Agent = unconfigured；三库存在 |
| 损坏 `agents.json` | 客户端启动；顶部显示配置错误；三库存在 |

### 0.6 文件地图

| 文件 | 作用 |
|---|---|
| `harness/core.py` | 配置、RuntimeManager、ChannelManager、三库初始化、persona 读写 |
| `harness/app.py` | Phase 1A 客户端（总览/设置/频道/事件/数据库） |
| `harness/app_v0.py` | 旧客户端，保留历史 |
| `harness/phase1a_check.py` | Phase 1A 验收脚本 |
| `harness/phase1b_check.py` | Phase 1B 验收脚本（fake CLI，21 项） |
| `harness/model_smoke.py` | 两 Agent 真实模型调用（需本机 key） |
| `harness/production_smoke.py` | 走 web Runtime 的真实请求 + 临时 Session Cancel |
| `harness/cancel_smoke.py` | 临时 Session 的真实 Cancel 路由验收 |
| `harness/run_live.ps1` | 在已设 key 的终端里跑上面两个 smoke |
| `harness/bilibili_source.py` | B 站 Source Adapter（真实 + fixture） |
| `harness/research_store.py` | research.db：sources/observations/memories |
| `harness/research_pipeline.py` | 字幕优先/视频理解回退/失败重试 |
| `harness/phase2_check.py` | Phase 2 离线 3 视频 E2E（18/18） |
| `harness/fixtures/research_favorites.json` | 3 视频离线 fixture |
| `tools/research_sync.py` | 收藏夹选择与同步 CLI |
| `PHASE-3A-REPORT.md` | Phase 3A 验收报告 |
| `PHASE-3B-REPORT.md` | Phase 3B 准备/真实运输报告 |
| `PHASE-3C-REPORT.md` | Phase 3C owner 私聊 reactive loop |
| `PHASE-4A-REPORT.md` | Phase 4A PySide6 Product Shell |
| `PHASE-4B-REPORT.md` | Phase 4B Windows onedir / 生命周期 / 真实按钮验收 |
| `PHASE-4C-AUDIT.md` | Phase 4C UI audit + wireframe |
| `PHASE-4C-REPORT.md` | Phase 4C Fish Design System / 六页改版 |
| `fish_desktop/theme/` | design tokens + 集中 QSS |
| `fish_desktop/widgets/` | Card / Avatar / MessageBubble / StatusPill / icons |
| `fish_desktop/assets/` | brand placeholder + Lucide icons |
| `harness/qq_agent_loop.py` | Fish Core orchestration |
| `harness/dsh_web.py` | DSH session/prompt 客户端 |
| `harness/qq_store.py` | qq.db conversation/message/turn |
| `channels/fish_qq/real_check.py` | Core 侧真实运输探针 |
| `channels/fish_qq/` | 独立 fish-qq：server/onebot/normalize/policy/spool |
| `PHASE-2-REPORT.md` | Phase 2 验收报告 |
| `PHASE-1B-REPORT.md` | Phase 1B 验收报告 |
| `harness/runtime/fish-control/` | 最小控制插件：`/fish/cancel` |
| `harness/agents.json` | Runtime + Agent + Channel 配置 |
| `harness/guard.py` | 规则闸门（实验，不接发送路径） |
| `tools/kb.py` | 知识库 CLI |
| `CHANNEL-SPEC.md` | fish-qq 接口契约（Part 2） |
| `IMPLEMENTATION_PLAN.md` | Phase 1A/1B 计划（Part 1） |
| `CURRENT_STATE.md` | Phase 0 现状核对（Part 3） |
| `napcat/` | NapCat Shell + 启动器 + Desktop MSI |
| `data/` | research.db / qq.db / shared |
| `rea-lab/ANALYSIS.md` | 参考项目拆解（Part 6） |

### 0.7 快速开始

```powershell
cd F:\Downloads\dsh-personal

# Phase 1A 客户端（不需要 DSH 也能启动）
python harness\app.py

# 验收
python harness\phase1a_check.py
```

### 0.8 当前待办（Phase 4C 已交付）

Phase 4A / 4B / 4C 已完成：PySide6 Product Shell、Windows onedir、Fish Design System、六页中文改版、`大鱼 Fish` 标题、真实按钮验收。

后续候选（未开始）：

1. 用户明确指定后才进入 Memory / proactive / Browse 等阶段。
2. 群智能行为不在本阶段范围；`FISH_QQ_GROUP_REPLY` 默认 off。
3. 品牌头像正式素材、代码签名、安装器、自动更新属于后续产品化。

---
"""

PARTS = [
    ("Part 1 · Phase 1A / 1B 实施计划", ROOT / "IMPLEMENTATION_PLAN.md", "md"),
    ("Part 2 · fish-qq Channel Adapter 规范", ROOT / "CHANNEL-SPEC.md", "md"),
    ("Part 3 · Phase 0 现状核对", ROOT / "CURRENT_STATE.md", "md"),
]

TAIL = r"""
---

# Part 4 · 大肥鱼人格与治理

> 当前运行时人格：`qq-work/AGENTS.md`（已用 `<owner-qq>` 填充主人）。
> 人格卡与治理参数如下；Phase 3 接入 fish-qq 后由 Fish Core 执行。

## 4.1 人格卡

```yaml
__PERSONA__
```

## 4.2 治理参数

```yaml
__POLICY__
```

## 4.3 运行时人格（qq-work/AGENTS.md）

__AGENTS__

---

# Part 5 · NapCat 部署现状

__NAPCAT__

---

# Part 6 · 参考项目拆解

__ANALYSIS__

---

# Part 7 · 历史与辅助材料

以下文件**不再是实现依据**，只作历史参考：

| 文件 | 状态 | 说明 |
|---|---|---|
| `MASTER-PLAN.md` | 历史 | 早期总体方案；Research 自主刷、digest、Guard 等已被 Handoff v2 覆盖 |
| `PRODUCT.md` | 历史 | 早期产品构想；Meme Vault、Content Filter 等已改为 V0 不做 |
| `ARCHITECTURE.md` | 历史 | 双实例隔离的原始设计；仍可读，但 Runtime/Channel 分层以本文件为准 |
| `qq/PERSONA-FRAMEWORK.md` | 历史 | 早期人格框架；部分被 Handoff v2 与 CHANNEL-SPEC 覆盖 |
| `dashboard/` | 已停用 | 旧网页监控面板；客户端改为桌面程序 |
| `harness/app_v0.py` | 保留 | Phase 1A 之前的客户端；不再作为主入口 |
| `qq/onebot.patch.yml` | 参考 | `dsh-onebot-qq` 配置示例；Phase 3 用 `fish-qq`，不再作为运行配置 |

## 7.1 当前实现边界（Phase 4C）

- Guard 规则引擎可运行（7/7 自测），但 4 条正常表达会被误杀；**没有接进发送路径**。
- `dsh-onebot-qq` 未安装，只作参考。
- fish-qq：Phase 3B 真实 Transport 主链完成；Phase 4A/4B/4C 只做桌面控制面、打包与视觉。
- NapCat：bot `1417334937` 已登录；OneBot `127.0.0.1:3001` 已配置；仅探测，不结束用户进程。
- Bilibili Source Adapter：Phase 2 完成（离线 18/18 + 真实「大肥鱼观看」3/3）。
- Phase 4C：Fish Design System + Lucide + 中文六页 + `大鱼 Fish`；群回复默认 off。
- 群聊智能行为、长期 Memory、主动插话、Browse 仍未做。

## 7.2 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-17 | Phase 1A 完成；Channel 改为自写 fish-qq；Phase 1A/1B 拆分；Runtime 配置与 Agent 字段分离 |
| 2026-10-17 | NapCat v4.18.38 Shell 解压；Desktop 3.5.0 MSI 下载并校验 |
| 2026-10-17 | `mcp-video-analyzer@0.11.0` 接口确认 |
| 2026-10-17 | Guard 误杀实测；V0 不接 Content Filter |

---

# Part 8 · Phase 1B 验收报告

__REPORT__

---

# Part 9 · Phase 2 验收报告

__PHASE2__

---

# Part 10 · Phase 3A 验收报告

__PHASE3A__

---

# Part 11 · Phase 3B 准备/验收报告

__PHASE3B__

---

# Part 12 · Phase 3C Private Reactive Agent Loop

__PHASE3C__

---

# Part 13 · Phase 4A Fish Desktop Product Shell

__PHASE4A__

---

# Part 14 · Phase 4B Desktop Productization

__PHASE4B__

---

# Part 15 · Phase 4C Fish Desktop UI/UX Redesign

__PHASE4C__
"""


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> None:
    chunks = [HEADER]
    for title, path, _kind in PARTS:
        chunks.append(f"\n---\n\n# {title}\n\n")
        chunks.append(read(path).strip() + "\n")

    tail = TAIL
    tail = tail.replace("__PERSONA__", read(ROOT / "qq" / "persona.yml").strip())
    tail = tail.replace("__POLICY__", read(ROOT / "qq" / "policy.yml").strip())
    tail = tail.replace("__AGENTS__", read(ROOT / "qq-work" / "AGENTS.md").strip())
    tail = tail.replace("__NAPCAT__", read(ROOT / "napcat" / "README.md").strip())
    tail = tail.replace("__ANALYSIS__", read(Path(r"F:\Downloads\rea-lab\ANALYSIS.md")).strip())
    tail = tail.replace("__REPORT__", read(ROOT / "PHASE-1B-REPORT.md").strip())
    tail = tail.replace("__PHASE2__", read(ROOT / "PHASE-2-REPORT.md").strip())
    tail = tail.replace("__PHASE3A__", read(ROOT / "PHASE-3A-REPORT.md").strip())
    tail = tail.replace("__PHASE3B__", read(ROOT / "PHASE-3B-REPORT.md").strip())
    tail = tail.replace("__PHASE3C__", read(ROOT / "PHASE-3C-REPORT.md").strip())
    tail = tail.replace("__PHASE4A__", read(ROOT / "PHASE-4A-REPORT.md").strip())
    tail = tail.replace("__PHASE4B__", read(ROOT / "PHASE-4B-REPORT.md").strip())
    tail = tail.replace("__PHASE4C__", read(ROOT / "PHASE-4C-REPORT.md").strip())
    chunks.append(tail)

    out = ROOT / "FISH-HARNESS-COMPLETE.md"
    text = "\n".join(chunks).rstrip() + "\n"
    for old, new in (
        ("[CHANNEL-SPEC.md](CHANNEL-SPEC.md)", "Part 2"),
        ("CHANNEL-SPEC.md", "Part 2"),
        ("IMPLEMENTATION_PLAN.md", "Part 1"),
        ("CURRENT_STATE.md", "Part 3"),
        ("ANALYSIS.md", "Part 6"),
        ("MASTER-PLAN.md", "MASTER-PLAN（历史）"),
        ("PRODUCT.md", "PRODUCT（历史）"),
        ("ARCHITECTURE.md", "ARCHITECTURE（历史）"),
    ):
        text = text.replace(old, new)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
