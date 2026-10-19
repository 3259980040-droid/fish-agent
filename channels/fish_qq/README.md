# fish-qq

独立 Python 进程，只做 QQ Transport；不依赖 DSH / dsh-onebot-qq。

```powershell
$env:FISH_QQ_TOKEN = "core-token"
$env:NAPCAT_TOKEN = "napcat-token"
python -m channels.fish_qq --config channels\fish_qq\config.json
```

服务：

```text
NapCat (OneBot 11 WS)  ←  fish-qq  →  ws://127.0.0.1:3101/ws  →  Fish Core
```

测试（Fake NapCat，不需要 QQ）：

```powershell
python -m unittest channels.fish_qq.tests.test_contract -v
```

边界：不做人格、相关性×活跃度、QQ Memory、Browse Session、正常发言预算；这些在 Fish Core。
