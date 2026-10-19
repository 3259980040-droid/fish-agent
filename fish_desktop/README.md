# Fish Desktop 4A / 4B

PySide6 Product Shell。UI 只通过 `fish_desktop/services.py` 调用现有
RuntimeManager / fish-qq / QQStore / ResearchStore；不在 UI 重写业务逻辑。

## 启动

正式 Windows 包：

```powershell
.\dist\Fish\Fish.exe
```

开发：

```powershell
cd F:\Downloads\dsh-personal
python -m fish_desktop.app
```

截图（offscreen，不依赖当前桌面会话）：

```powershell
python -m fish_desktop.app --screenshot .\screenshots
```

## 生命周期

- 单实例：Windows 命名 mutex；再次启动直接退出。
- Start Fish：只启动 Fish-owned fish-qq / qq_agent_loop / DSH；端口已开则不重复启动。
- Stop Fish：只停 3090/3101/3102；NapCat/QQ 3001 永不结束。
- 关闭窗口：V0 = 退出客户端 + 通过 Windows Job Object 停止 Fish-owned 子进程；NapCat 保留。
- 群回复默认 `FISH_QQ_GROUP_REPLY=off`。

## 打包

```powershell
python -m PyInstaller --noconfirm --clean Fish.spec
```

产物：

```text
dist/Fish/Fish.exe
dist/Fish/_internal/
```

同一个 `Fish.exe` 也作为 backend：

```text
Fish.exe --backend fish-qq --config ...
Fish.exe --backend qq-agent-loop
```

打包不包含数据库、API key、token、cookie。运行时会从现有项目目录和 User 环境读取。

`Fish.spec` 排除 Qt 自带 `icuuc.dll`，让 Qt6Core 使用 Windows 系统 ICU；这是当前 Windows 环境依赖。
