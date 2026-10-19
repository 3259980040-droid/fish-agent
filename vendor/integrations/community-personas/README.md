# Fish 社区人格预设

此目录保存从实际上游源码提取的人格数据及对应许可证，不执行上游项目。

| 项目 | 提取内容 | 来源 |
| --- | --- | --- |
| QQ Agent | 小鲸鱼完整角色卡 | https://github.com/K0nd1us/QQ-agent/blob/fdfd325e148d91845a0aeea3dfdff4cf92fc61a1/src/personas.js |
| MaiBot | PersonalityConfig 的 personality、behavior_style、reply_style 默认值 | https://github.com/Mai-with-u/MaiBot/blob/ff0fe42500f5f7581e9655d1e78379407233207a/src/config/official_configs.py |
| AstrBot | DEFAULT_PERSONALITY 默认助手正文 | https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/persona_mgr.py |

源码读取日期为 2026-10-20。QQ Agent 为 MIT，MaiBot 为 GPL-3.0，AstrBot 为 AGPL-3.0；完整许可文件在同目录。AstrBot 默认正文很短，它不是另一套小鲸鱼角色卡。

`personas.json` 是预先提取的数据。`tools/build_community_personas.py` 从研究快照静态提取字符串，重建此文件；不导入或运行外部代码。导入后在 Fish 本地形成新的不可变版本，来源写入人格数据库。宿主的工具、权限、消息发送与定时器由 Fish / DSH 接入层提供；外部角色卡中的工具名不自动成为可执行能力。
