# 当前实施入口

唯一规格：`F:/AIWork/DSH/项目/DSH-Fish/设计/实施规格/Fish_统一Agent系统更新_实施规格_2026-10-24.md`。
工单索引：`F:/AIWork/DSH/项目/DSH-Fish/实施/统一Agent系统更新/工单索引.md`。
代码位置／退役对照：`F:/AIWork/DSH/项目/DSH-Fish/设计/功能说明/Fish_任务表_合并最终版_2026-10-24.md`。

实际源码保持当前根；前端在同宿主的 `source/dsh/packages/client/ui-fish`。M01 搬根仍延期。
执行进度记录到 `F:/AIWork/DSH/项目/DSH-Fish/实施/统一Agent系统更新/实施记录/`，文件按所属票分类。

当前实施顺序按唯一规格§8.10的2026-10-26九票依赖：W01起步，随后R01—R05，W02、W03、W04依实际前置推进；原21张为既有实现，不重做。原父项历史状态保留。本轮最终由09-10集中一次客户端＋主人私聊验收，07-01仅接真实运行结果，群只读/临时根。主人此前已授权必要实施、API和重载；每轮仍按当轮范围执行，主人已调用implement-spec，当前按九票整合分支实施；各票必要离线核对后整组集中验收。

保留当前工作树已有修改；GitHub 初始上传仅包含工作代码与当前规格工单，排除凭据、生产数据、私聊原文、输出日志及旧历史中的敏感内容。上传前审查实际清单，不直接整盘上传。

## 实例位置

`locations.InstanceContext`明确宿主根、稳定实例ID和实例目录。旧root/data等调用已改用显式`project_path`，不复制Store算法，也不切换进程环境变量。

初始实例`main`归在`data/instances/main`；首次新版本初始化在停服维护时搬接既有可变数据，`data/host/initial-migration.json`记录实际移动及结构化文件引用更新。程序、端口、模型凭据、账号连接、MCP应用保留宿主位置；旧测试space仅保留为初始实例历史资料，不当作新实例。当前票只在临时根执行迁接，生产维护留最终部署。

窗口工作台动作以params.instance_id定位，`instances.list`为宿主动作。所有入口未显式指定时只落稳定初始实例，永远不从当前前端选中值推断目标。

## QQ执行结果监听

`QQAgentLoop.watch_turn(execution, source=..., chat=..., instance=runtime.context)`在QQ事件循环调用，返回已创建的Task；也接受实例root。监听固定该实例的状态请求和管理员日志，完成后自动移出生命周期集合，QQ停机由原stop取消并等待。阻塞提交在线程结束后回到事件循环再调监听；私聊沿watch_private使用同一入口。

W03逐实例保存原消息、提交后，用对应runtime.context和execution调用此入口。监听只读取原执行记录并写所属实例管理员日志，不改QQStore消息；原消息的runtime_turn_id等写回由提交方使用所属实例QQStore负责，不使用共享连接的默认Store覆写另一实例记录。开始/结束、工具与投递事实仍由原turn_view/trace读取。
