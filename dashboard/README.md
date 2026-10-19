# 大肥鱼 Mission Control

一个本地只读监控面板，把两个实例和 QQ 群的运行状况放在一页里。

## 启动

```powershell
cd F:\Downloads\dsh-personal
python dashboard\server.py --port 3099
```

打开 <http://127.0.0.1:3099>。
演示数据：<http://127.0.0.1:3099/?demo=1>。

## 看什么

| 卡片 | 数据来源 |
|---|---|
| 实例 | 探活 `127.0.0.1:3080`（研究）和 `:3090`（QQ） |
| 今日预算 | `qq-work/state.json` 的 `qq.today.cost_cny` / `qq.budget` |
| 大肥鱼 | 同上的模式、群、今日轮/回/媒体、最后一句 |
| 知识库 | `knowledge/index.json` 的笔记数、今日新增、标签 |
| 今日日报 | `shared/digest/latest.md` 的修改时间与预览 |
| 告警 | 端口离线、预算超 80%、日报超 26h、B 站验证、主人未配置 |
| 群聊动态 | `qq-work/state.json` 的 `qq.feed`，没有就读 `qq-work/log/events.jsonl` |

## 让 QQ 实例写数据

`qq-gate`（v1）或 agent 每轮回写一次 `qq-work/state.json`：

```json
{
  "qq": {
    "mode": "active",
    "online": true,
    "group": "123456789",
    "owner": "10001",
    "owner_configured": true,
    "bilibili_blocked": false,
    "today": { "turns": 42, "replies": 11, "media": 2, "cost_cny": 0.31 },
    "budget": { "cny_per_day": 5, "hard_stop_cny": 4.5 },
    "last_reply": { "at": "20:41", "text": "等等党又输一年。" },
    "feed": [
      { "at": "20:41", "user": "阿岚", "text": "这显卡又涨价了", "reply": "等等党又输一年。" }
    ]
  }
}
```

没有这个文件时，面板仍能显示端口、知识库、日报，QQ 卡片为零。

## 边界

- 只读：没有控制按钮，改配置还是去 `qq/policy.yml`。
- 只绑 `127.0.0.1`：不要暴露到公网。
- 零依赖：Python 3.10+ 标准库，不需要 npm / pip。
- 5 秒轮询：够用，不做 WebSocket。
