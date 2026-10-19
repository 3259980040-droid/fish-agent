# Fish 使用的开源组件

Fish 是个人集成框架，不是 DeepSeek 官方客户端。模型和图像理解由配置的服务商提供。

| 用途 | 组件及版本 | 来源 / 许可证文件 |
| --- | --- | --- |
| Agent、会话、模型流和原生插件 | DeepSeek Harness 0.2.1-rc.2 | https://github.com/deepseek-ai/deepseek-harness · 随包 runtime/dsh/node_modules/@deepseek-ai/dsh/LICENSE |
| 桌面界面 | PySide6 / Qt 6.12.0 | https://www.qt.io/qt-for-python · 随包 licenses/PySide6* |
| MCP 客户端 | 官方 MCP Python SDK 1.30.0 | https://github.com/modelcontextprotocol/python-sdk · 随包 licenses/mcp |
| B 站读取 | bilibili-api-python 17.4.2 | https://github.com/Nemo2011/bilibili-api · 随包 licenses/bilibili-api-python |
| 登录流程参考 | dsh-bili-scroll | https://github.com/c1overzz/dsh-bili-scroll · 源码 vendor/sources/dsh-bili-scroll 保留 MIT 许可证 |
| 视频抽帧、OCR、字幕提取 | mcp-video-analyzer 0.11.0 及其依赖 | https://github.com/guimatheus92/mcp-video-analyzer · 随包 runtime/video/node_modules 中各组件许可证 |
| 视频底层工具 | ffmpeg-static 中的 Windows ffmpeg | 随包 runtime/ffmpeg-license.txt；具体二进制哈希见 runtime/manifest.json；上游 https://github.com/eugeneware/ffmpeg-static |
| JavaScript 运行时 / 包管理 | Node.js 24.21.0 / pnpm 11.7.0 | runtime/node/LICENSE；runtime/pnpm/LICENSE |
| UI 方法参考 | Refactoring UI Skill | https://github.com/edisonmbli/refactoring-ui-skill · 安装于 Codex skills，未作为 Fish 运行依赖 |

完整 Python 组件版本和许可证文本放在随包 `licenses/`；npm 依赖保留在随包 `runtime/**/node_modules/`，其包内许可证一并保留。依赖锁位于源码 `packaging/python-runtime-lock.txt`、`vendor/runtime/dsh/package-lock.json` 和 `vendor/runtime/video/package-lock.json`。

Fish 自己负责桌面交互、角色与数据分区、主人身份和权限核验、本地输出拦截、工具桥接、记忆/表情库、研究队列及备份升级。持续保留第三方名称与来源，不把已有开源能力标成自主开发。
