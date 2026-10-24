# Fish 工程约定

默认简体中文。保留用户现有改动和部署位置，按当前有效规格与工单实施。

## Agent skills

### Issue tracker

目标为用户 GitHub 下独立私有 Fish 仓库；远端尚待创建，连接未完成期间使用已存在的本地工单，不生成第二套。见 `docs/agents/issue-tracker.md`。

### Triage labels

采用默认五种标签，记录含义及远端创建状态。见 `docs/agents/triage-labels.md`。

### Domain docs

single-context：根 `GLOSSARY.md` 与 `docs/adr/`。术语或架构变更时按 `docs/agents/domain.md` 读取。

## 实施入口

当前唯一规格、本地工单、源码及前端位置见 `docs/agents/execution.md`。接票先读关联条款和实际调用者，修改负责模块并退出被替代路径。

用户已授权本批必要代码修改、API 调用、构建、客户端／后台重载和 GitHub 上传；技术选择自行处理，用户只验收最终结果。整套完成后集中做一次主人私聊及客户端体验，群只读或临时根模拟。按改动需要做必要静态检查，避免逐票重复模型实验或额外故障矩阵。
