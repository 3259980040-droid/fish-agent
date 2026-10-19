# fish-control

Fish Core 的本地控制和强制工具拦截插件（0.2.0）。

- `GET /fish/status`：返回活跃 Session 列表。
- `GET /fish/connect`：经令牌认证后返回当前进程的会话连接入口，供桌面原生图片聊天使用。调用方不得打印或持久化返回的认证 URL。
- `POST /fish/cancel?session_id=<id>`：按 session 取消。
- 多个 active 且未指定 `session_id`：返回 `409 multiple`，不随机取消。
- 没有 active：返回 `404 none`。
- 只绑定 DSH 的本地 webServer；token 从 `FISH_CONTROL_TOKEN` 读。
- 令牌缺失时拒绝请求。`FISH_HARNESS_HOME` 和 `FISH_AGENT_ID` 必须存在。
- 使用运行时正式的 `tools.guard()` 注册不可被普通预执行监听器放行的阻断器，按角色限制工具、文件路径、暂停状态与每轮工具次数。配置读取失败时拒绝工具执行。

## 安装

```powershell
$env:DSH_HOME = "<home>"
dsh plugin --profile web add F:\Downloads\dsh-personal\harness\runtime\fish-control
```

Phase 1B 由 `RuntimeManager.ensure_control_plugin()` 自动尝试安装。
