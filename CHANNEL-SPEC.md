# fish-qq Channel Adapter · 接口与生命周期规范 v1

> 状态：Phase 3A 离线实现 + Fake NapCat 契约 15/15；真实 NapCat / QQ 尚未验证。
> 角色：`fish-qq` 是独立 Python 进程，只做 QQ Transport、allowlist、基础限频、事件规范化、发送。
> 不承载：人格、相关性、活跃度、Agent Memory、网络搜索。
> 参考实现：`dsh-onebot-qq`（只读参考，不作为运行依赖）。

---

## 1. 进程与连接拓扑

```
NapCat (OneBot 11)
   ▲  ws://127.0.0.1:3001   （fish-qq 作为客户端拨出）
   │
fish-qq  ── ws://127.0.0.1:3101/ws ──  Fish Core
（WS Server）                              （WS Client）
   │
   └─ channel-state/  轻量 spool / lock / log
```

- `fish-qq` **监听** `127.0.0.1:3101`，Fish Core 连进来。
- `fish-qq` **拨出** 到 NapCat 的正向 WebSocket `ws://127.0.0.1:3001`。
- 两条连接互相独立：Core 断开不影响 QQ 连接；NapCat 断开不影响 Core 与 fish-qq 的控制通道。
- 只绑回环地址，不监听 `0.0.0.0`。

---

## 2. 帧格式

所有帧为 UTF-8 JSON 文本，单帧上限 1 MiB。

```json
{
  "v": 1,
  "type": "hello | hello.ok | event | action | action.result | ping | pong | error",
  "id": "可选，action/result 关联 id",
  "ts": "2026-10-17T22:10:00+08:00",
  "payload": {}
}
```

- `v` 固定 `1`；不兼容变更升 `2`。
- `id` 只用于请求/响应关联，由 Core 生成；事件不带 `id`。
- 所有时间戳为带时区的 ISO 8601。

---

## 3. 握手与鉴权

### 3.1 Core → fish-qq

WebSocket 握手带：

```http
Authorization: Bearer <FISH_QQ_TOKEN>
```

连接建立后第一帧：

```json
{
  "v": 1,
  "type": "hello",
  "id": "h1",
  "ts": "...",
  "payload": {
    "client": "fish-core",
    "last_seq": 0,
    "epoch": null
  }
}
```

### 3.2 fish-qq → Core

```json
{
  "v": 1,
  "type": "hello.ok",
  "id": "h1",
  "ts": "...",
  "payload": {
    "channel": "qq",
    "state": "ready",
    "bot": { "id": "<BOT_QQ>", "display_name": "大肥鱼" },
    "epoch": "9f2c...",
    "seq": 0,
    "capabilities": ["send_text", "send_image", "reply", "history", "native_face_probe"],
    "limits": {
      "max_text_chars": 4000,
      "max_segments": 20,
      "max_frame_bytes": 1048576
    }
  }
}
```

鉴权失败：返回 `error`（`unauthorized`）后关闭，WebSocket close code `4401`。
握手 10 秒未完成：close `4408`。

---

## 4. 规范化标识

| 字段 | 格式 | 说明 |
|---|---|---|
| `chat_id` | `group:<group_id>` / `private:<user_id>` | 不向上层暴露 `message_type` |
| `message_id` | 字符串 | OneBot 原样，仅作不透明 id |
| `sender.id` | 字符串 | QQ 号 |
| `sender.display_name` | 字符串 | 群名片/昵称，可为空 |
| `epoch` | UUID | fish-qq 进程启动时生成，重启后变化 |
| `seq` | 整数 | 本进程内单调递增，事件排序用 |

`chat_type` 仅 `group` / `private`，由 `chat_id` 前缀决定，不单独传递。

---

## 5. 事件（fish-qq → Core）

### 5.1 `channel.state`

```json
{
  "v": 1,
  "type": "event",
  "event": "channel.state",
  "seq": 1,
  "epoch": "9f2c...",
  "ts": "...",
  "payload": {
    "state": "ready | degraded | connecting | stopping",
    "reason": "napcat_connected | napcat_disconnected | handshake_timeout | ...",
    "since": "...",
    "napcat": { "connected": true, "url": "ws://127.0.0.1:3001" },
    "reconnected": false
  }
}
```

### 5.2 `message.received`

```json
{
  "v": 1,
  "type": "event",
  "event": "message.received",
  "seq": 12,
  "epoch": "9f2c...",
  "ts": "...",
  "payload": {
    "chat_id": "group:123456789",
    "chat_type": "group",
    "message_id": "1234567890",
    "sender": { "id": "<owner-qq>", "display_name": "主人" },
    "segments": [
      { "type": "text", "text": "在吗" },
      { "type": "at", "user_id": "<owner-qq>" }
    ],
    "mentions_bot": true,
    "reply_to_bot": false,
    "reply_to": null,
    "received_at": "..."
  }
}
```

- `mentions_bot` / `reply_to_bot` 是协议层归一化，不是人格判断。
- `segments` 只允许规范化类型（见 §7）；不允许原始 OneBot 段。
- 不在事件里带原始 JSON、CQ 字符串、本地文件路径。

### 5.3 `channel.metrics`（可选，60 秒一次）

```json
{
  "v": 1,
  "type": "event",
  "event": "channel.metrics",
  "seq": 13,
  "epoch": "9f2c...",
  "ts": "...",
  "payload": {
    "received": 120,
    "dropped_allowlist": 30,
    "dropped_dup": 2,
    "buffer_overflow": 0,
    "unknown_segments": 1,
    "sent": 18,
    "failed": 1,
    "reconnects": 2,
    "uptime_s": 3600
  }
}
```

---

## 6. 动作（Core → fish-qq）

每个动作一个 `action` 帧，一个 `action.result` 帧。

### 6.1 `send_message`

```json
{
  "v": 1,
  "type": "action",
  "id": "a1",
  "action": "send_message",
  "ts": "...",
  "payload": {
    "chat_id": "group:123456789",
    "segments": [{ "type": "text", "text": "在，刚翻了个身。" }],
    "reply_to": "1234567890",
    "idempotency_key": "fish-20261017-0001"
  }
}
```

成功：

```json
{
  "v": 1,
  "type": "action.result",
  "id": "a1",
  "ok": true,
  "ts": "...",
  "payload": {
    "chat_id": "group:123456789",
    "message_id": "1234567891",
    "sent_at": "..."
  }
}
```

失败：

```json
{
  "v": 1,
  "type": "action.result",
  "id": "a1",
  "ok": false,
  "ts": "...",
  "error": {
    "code": "rate_limited",
    "message": "outbound per-chat limit",
    "retry_after_ms": 45000,
    "may_have_sent": false
  }
}
```

### 6.2 `get_history`

```json
{
  "v": 1,
  "type": "action",
  "id": "a2",
  "action": "get_history",
  "payload": {
    "chat_id": "group:123456789",
    "limit": 30,
    "before_message_id": null
  }
}
```

结果：

```json
{
  "v": 1,
  "type": "action.result",
  "id": "a2",
  "ok": true,
  "payload": {
    "messages": [
      {
        "chat_id": "group:123456789",
        "chat_type": "group",
        "message_id": "...",
        "sender": { "id": "...", "display_name": "..." },
        "segments": [{ "type": "text", "text": "..." }],
        "mentions_bot": false,
        "reply_to_bot": false,
        "reply_to": null,
        "received_at": "..."
      }
    ]
  }
}
```

### 6.3 `get_status`

```json
{ "v": 1, "type": "action", "id": "a3", "action": "get_status", "payload": {} }
```

结果：

```json
{
  "v": 1,
  "type": "action.result",
  "id": "a3",
  "ok": true,
  "payload": {
    "state": "ready",
    "bot": { "id": "...", "display_name": "..." },
    "napcat": { "connected": true, "url": "ws://127.0.0.1:3001" },
    "epoch": "9f2c...",
    "seq": 42,
    "uptime_s": 3600,
    "version": "0.1.0",
    "queue": { "outbox": 0 },
    "counters": { "received": 120, "sent": 18, "failed": 1 }
  }
}
```

### 6.4 `probe_capabilities`（Phase 6，接口先留）

```json
{ "v": 1, "type": "action", "id": "a4", "action": "probe_capabilities", "payload": {} }
```

结果：

```json
{
  "v": 1,
  "type": "action.result",
  "id": "a4",
  "ok": true,
  "payload": {
    "native_face": {
      "fetch": true,
      "detail": true,
      "add": true,
      "delete": true,
      "set_desc": true,
      "send": null
    }
  }
}
```

`send: null` 表示未验证；不要预设支持。

### 6.5 `ping` / `pong`

```json
{ "v": 1, "type": "ping", "id": "p1", "ts": "..." }
{ "v": 1, "type": "pong", "id": "p1", "ts": "..." }
```

---

## 7. 规范化消息段

| type | 字段 | V0 |
|---|---|---|
| `text` | `text` | ✓ |
| `image` | `url`（http/https）、`alt?` | ✓ |
| `at` | `user_id` | ✓ |
| `reply` | 归一化为消息级 `reply_to`，不作为段 | ✓ |
| `face` | `id`、`text?` | 仅入站归一化；出站暂不支持 |
| `unknown` | `raw_type`（仅类型名）、`placeholder` | 入站保留占位，不静默丢弃 |

规则：

- 出站只接受 `text` / `image`；`at` / `face` 在 Phase 3.5 再开。
- `image.url` 必须是 `http(s)`；不接受本地路径、`base64://`、`file://`。
- **不支持的入站段不丢弃**：归一化为
  `{ "type": "unknown", "raw_type": "<类型名>", "placeholder": "[不支持的消息类型: <类型名>]" }`，
  让 Core 能看到“这里有一条消息，只是格式不认识”。
- 出站遇到不支持类型：返回 `unsupported_segment`，不发送。
- 单条 `text` 超过 `max_text_chars`：拒绝 `payload_too_large`，不截断。
- 单条消息最多 `max_segments` 段。

---

## 8. allowlist、限频、去重（fish-qq 内）

### 8.1 allowlist

配置：

```json
{
  "allowlist": {
    "owner_id": "<owner-qq>",
    "group_ids": ["<群号>"],
    "private_mode": "owner_only"
  }
}
```

- 私聊：只允许 `owner_id`；其他私聊直接丢弃，不产生事件。
- 群聊：只允许 `group_ids`；空数组 = 拒绝所有群（fail-closed）。
- 丢弃只计数，不回发错误，不向 Core 泄漏内容。

### 8.2 限频

| 方向 | 默认 | 行为 |
|---|---|---|
| 入站 | **不按分钟丢弃** | 所有 allowlist 消息进入 500 条 / 5 分钟短期 buffer；buffer 满时丢最旧并计数，不因 30/min 丢弃 |
| 出站（技术保护） | 每 chat 20 条/分钟 | Channel 技术上限，**不是大肥鱼正常发言预算**；超出返回 `rate_limited` |
| 出站全局（技术保护） | 60 条/分钟 | 同上 |

**入站不设 30/min 丢弃**。群消息再多，也要尽可能完整进入短期 buffer；
真正的限制在 Fish Core：Agent invocation、reply、browse 的预算与活跃度。

`20/min/chat`、`60/min global` 只是 Channel 的**技术保护上限**，防止刷屏打爆 NapCat；
大肥鱼“每天说几句、每小时说几句”由 Fish Core 的预算层决定，两者不要混。

### 8.3 去重

- 按 `message_id` 去重，窗口 300 秒。
- 重复事件丢弃并计数。

---

## 9. 进程生命周期

### 9.1 状态机

```
starting
  → connecting        （开始连 NapCat）
  → ready             （NapCat 已连接 + 本地 WS 已监听）
  → degraded          （NapCat 断开）
  → ready             （重连成功）
  → stopping          （收到 SIGINT/SIGTERM）
  → stopped
  → failed            （配置错误/端口占用/无法恢复）
```

### 9.2 启动顺序

1. 读取并校验配置；失败 → `failed`，退出码 2。
2. 获取 `channel-state/fish-qq.lock`（PID）；已有活进程 → 退出码 3。
3. 打开 `channel-state/`，恢复未完成的 outbox（见 §10）。
4. 监听 `127.0.0.1:3101/ws`。
5. 拨出 NapCat `ws://127.0.0.1:3001`，带 token。
6. 拉取 bot 信息；生成 `epoch`；状态 `ready`；向已连接的 Core 发 `hello.ok` / `channel.state`。

### 9.3 关闭顺序

1. 状态 `stopping`；拒绝新动作（`not_ready`）。
2. 等待在途发送完成，最多 5 秒。
3. 关闭 NapCat WS（code 1000）。
4. 关闭 Core WS（code 1001）。
5. 释放 lock；退出码 0。

### 9.4 崩溃恢复

- lock 文件记录 PID + 启动时间；PID 不存在则视为陈旧锁，覆盖。
- `outbox.jsonl` 只保存**已接受、未确认**的出站动作：
  `{id, action, payload, created_at, expires_at, status: queued|inflight|confirmed|unknown}`。
- 状态定义：
  - `queued`：已接受，尚未提交给 NapCat；崩溃恢复后**可以自动重试**。
  - `inflight`：已提交给 NapCat，尚未收到确认；崩溃恢复后**一律转 `unknown`，不自动重试**（个人 QQ 重复发消息比漏发更糟）。
  - `confirmed`：已收到 `message_id`，终态。
  - `unknown`：可能已发出，等 Core 用 `idempotency_key` 决定；不自动重试。
- 过期：标 `expired`，不发送。
- 入站事件不做持久化；只在内存 ring buffer 保留最近 500 条 / 5 分钟，供 Core 重连补发。
- **所有 allowlist 入站消息都进 buffer**，不因分钟频率被丢弃；buffer 满时丢最旧并计 `buffer_overflow`。

---

## 10. 错误与重连语义

### 10.1 错误码

| code | 含义 | 可重试 |
|---|---|---|
| `bad_request` | 帧格式/字段错误 | 否 |
| `unauthorized` | token 错误 | 否 |
| `not_ready` | fish-qq 正在启动/关闭 | 是，稍后 |
| `channel_unavailable` | NapCat 未连接 | 是，等 `channel.state` |
| `rate_limited` | 触发限频 | 是，按 `retry_after_ms` |
| `timeout` | 动作超时 | 视 `may_have_sent` |
| `send_failed` | NapCat 返回失败 | 视 `may_have_sent` |
| `unsupported_segment` | 段类型不支持 | 否 |
| `payload_too_large` | 超长/超段数 | 否 |
| `internal` | 未分类错误 | 视情况 |

`send_failed` / `timeout` 必须带 `may_have_sent`：

- `false`：可以安全重试；
- `true`：消息可能已发出，**禁止自动重试**，由 Core 决定是否让用户确认。

### 10.2 NapCat 断线

- 立即状态 `degraded`，发 `channel.state`。
- 所有 `send_message` 立即返回 `channel_unavailable`，不排队。
- 重连退避：1s、2s、4s、8s、16s、30s、60s，之后固定 60s；每次加 0–20% 抖动。
- 心跳：30s 发 ping，90s 无 pong 视为断线。
- 重连成功：重新拉 bot 信息；`channel.state` 带 `reconnected: true` 和 `gap_ms`。
- 断线期间的入站消息**不保证补发**；Core 需要时用 `get_history` 补。

### 10.3 Core 断线

- fish-qq 继续运行，NapCat 连接保持。
- 入站事件进入 ring buffer（500 条 / 5 分钟），不阻塞、不无限增长。
- Core 重连 `hello` 带 `last_seq` + `epoch`：
  - `epoch` 相同且 `last_seq` 在 buffer 内：补发缺失事件；
  - `epoch` 不同或太旧：不补发，发 `channel.state` 标 `gap: true`，由 Core 用 `get_history` 补齐。

### 10.4 发送超时与幂等

- 单次发送超时 15 秒。
- `idempotency_key` 缓存 10 分钟；重复 key 返回缓存结果，不重复发送。
- 超时且 `may_have_sent=true` 时，fish-qq 在 outbox 标 `unknown`，不自动重试。

---

## 11. 配置

`channels/fish_qq/config.json`：

```json
{
  "listen": {
    "host": "127.0.0.1",
    "port": 3101,
    "path": "/ws",
    "token_env": "FISH_QQ_TOKEN"
  },
  "napcat": {
    "url": "ws://127.0.0.1:3001",
    "token_env": "NAPCAT_TOKEN",
    "heartbeat_s": 30,
    "heartbeat_timeout_s": 90
  },
  "allowlist": {
    "owner_id": "<owner-qq>",
    "group_ids": [],
    "private_mode": "owner_only"
  },
  "limits": {
    "inbound_per_min": 30,
    "outbound_per_min": 20,
    "outbound_global_per_min": 60,
    "max_text_chars": 4000,
    "max_segments": 20,
    "max_frame_bytes": 1048576
  },
  "dedup_window_s": 300,
  "spool_dir": "channel-state",
  "log_level": "info"
}
```

token 从环境变量读，不写配置文件。

---

## 12. 安全与边界

- 只绑 `127.0.0.1`；不暴露公网。
- 本地 WS 必须带 token；token 不写日志。
- 不暴露任意 OneBot action；只允许上表动作。
- 不向上层传原始 OneBot JSON / CQ 字符串 / 本地路径。
- 日志默认脱敏：不记录消息正文，只记录 `chat_id`、`message_id`、段类型、长度、命中策略。
- 单帧 ≤1 MiB，段数 ≤20，文本 ≤4000 字。
- 不允许 Core 指定本地文件路径；图片只接受 http(s) URL。

---

## 13. 契约验收（实现后必须跑）

1. 无 token / 错 token：握手被拒，close 4401。
2. allowlist：非白名单群/非主人私聊不产生 `message.received`，计数增加。
3. 规范化：入站 CQ/段数组变成 `text`/`image`/`at`，没有原始 OneBot 字段。
4. 发送：文本、图片 URL、引用回复成功；返回 `message_id`。
5. 历史：`get_history` 返回 30 条，顺序正确。
6. 入站不丢：60 条/分钟的 allowlist 群消息全部进入 buffer/事件，不因频率丢弃。
7. 出站技术限频：第 21 条/分钟返回 `rate_limited`，带 `retry_after_ms`。
8. 断线：停 NapCat → `degraded`，发送返回 `channel_unavailable`；重启 → `ready` + `reconnected:true`。
9. Core 断线重连：`last_seq`/`epoch` 补发；epoch 变化时标 `gap`。
10. 幂等：同 `idempotency_key` 重发两次，只发一条。
11. 超时：模拟发送超时，`may_have_sent` 正确，不自动重试。
12. 不支持段：入站保留 `unknown` placeholder；出站返回 `unsupported_segment`，不泄漏原始内容。
13. 进程：SIGTERM 5 秒内退出；锁文件释放；崩溃后仅 `queued` 自动重试，`inflight` 转 `unknown`。
