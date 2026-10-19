# 插件

一个插件 = 一个目录 + 一个 `plugin.json`。

```
harness/plugins/
├── video-understanding/
│   ├── plugin.json
│   ├── server.py
│   └── data/
└── examples/
    └── video-understanding.json
```

## Manifest 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✓ | 唯一 id，小写短横线 |
| `name` | ✓ | 显示名 |
| `type` | ✓ | `mcp` / `tool` / `skill` / `panel` |
| `agents` | ✓ | 只对这些 Agent 生效：`research` / `qq` |
| `entry` | mcp/tool | `command` / `args` / `cwd` |
| `permissions` | ✓ | `filesystem` / `network` / `shell` |
| `config` | | 配置项 schema（v1 生成表单） |
| `tools` | | 暴露给 Agent 的工具名 |

## 规则

- 插件只对 `agents` 列出的 Agent 生效；QQ 实例默认拿不到视频插件。
- 权限最小化：要文件就只给 `data/`；`network: false` 就是断网。
- 插件跑子进程；崩溃不拖垮客户端，重启超过 3 次就停用并告警。
- 装插件 = 复制目录 + 校验 manifest + 重启对应 Agent。

## 视频理解怎么接

本机已装 `mcp-video-analyzer@0.11.0`，有两条接法：

1. **MCP server（推荐）**：`mcp-video-analyzer` 默认以 stdio MCP 启动，暴露
   `analyze_video` / `analyze_videos` / `get_transcript` / `get_metadata` / `get_frames` / `analyze_moment`。
   在 `plugin.json` 里：

   ```json
   "type": "mcp",
   "agents": ["research"],
   "entry": {
     "command": "C:\\Users\\江\\AppData\\Roaming\\npm\\mcp-video-analyzer.cmd",
     "args": []
   }
   ```

2. **CLI fallback**：`mcp-video-analyzer analyze <path> --detail standard --fields metadata,transcript,frames,ocrResults,timeline,aiSummary --out data/research/frames`。
   stdout 是单个 JSON，帧图复制到 `--out`；适合不想接 MCP 时直接调。

`agents` 只填 `["research"]`，QQ 实例拿不到这个工具。`network: false` 只对本地文件成立；
要分析 B 站/YouTube URL 时再单独放开。
