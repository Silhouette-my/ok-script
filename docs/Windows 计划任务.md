# Windows 计划任务

ok-script 支持把一次性任务（`onetime_tasks`）注册为 Windows 计划任务，
按计划自动运行。

## 任务定位机制：`-t N`

计划任务通过 `-t N` 参数定位要运行的任务，其中 `N` 是 `onetime_tasks`
列表中的 **1-based 索引**（例如 `-t 1` 表示列表第 1 个任务）。

```xml
<Arguments>main.py -t 1 -e</Arguments>
```

任务被创建时，`CreateScheduleTaskDialog` 用
`og.executor.onetime_tasks.index(selected_task) + 1` 计算索引并写入 XML。

### 索引重排问题

由于定位依赖的是**索引**而非任务名，一旦 `config.py` 中 `onetime_tasks`
的顺序发生变化（例如把业务任务前置、测试任务后置），**已创建**的计划任务
仍指向旧索引，就会运行到错误的任务（例如 `-t 15` 从「启动一次游戏」跑到
`TestDemoGraphic`）。

## 启动时自动校正与稳定标识迁移

为保留 ok 原生 `-t` 机制、不修改任何运行时解析 / 创建 / 修改对话框逻辑，
ok-script 在**每次启动时**自动校正并把 `-t` 目标统一迁移为**稳定标识**
（模块路径.类名，如 `src.tasks.onetime.DailyTask`，对排序免疫）：

- 挂载点：`MainWindow.__init__`（构造 ScheduleTaskTab、加载任务缓存之前，早于 `start_runtime`），原生内联调用
- 实现：`ok/ui/qt/tasks/schedule_index_sync.py`（同步逻辑），`MainWindow.__init__` 直接调用
  `sync_schedule_task_indexes()`
- 原理：解析缓存任务当前的 `-t` 目标（数字索引 / 历史任务名 / 模块路径.类名），
  以缓存任务名（创建时选择的任务名，不随排序变化）或已缓存的稳定标识为权威身份，
  在当前 `onetime_tasks` 中定位目标任务，把 `-t X` 统一改写为当前实际的
  `模块路径.类名`；同时回填缓存的 `task_index` / `task_identifier` 元数据。

校正规则：

1. 只处理本应用（如 `\ok-ef\`）下的任务，其它 ok-* 应用的只读任务不动；
2. 任务名与稳定标识都无法在当前 `onetime_tasks` 中唯一定位时跳过（可能已被删除）；
3. 目标与元数据均已正确时跳过（幂等，不写缓存、不调 COM）；仅缺元数据时只回填
   缓存，不重注册 Windows 任务；
4. 有变更时同步更新缓存 / `xml_config` / Windows 计划任务
   （COM `RegisterTaskDefinition`，失败回退 `schtasks /Create /XML /F`）；
5. 改写本次进程 `sys.argv` 与 `og.ok.args['task']`，保证本次启动也使用正确目标；
6. 每次进程只校正一次（进程级 guard）。

> 注意：纯 headless（无 GUI）的应用不经过 `showEvent` 挂载点，不会触发
> 自动校正，此时请使用下面的一次性工具。

## 一次性校正工具

当 GUI 无法正常启动，或需要手动执行校正时，可在应用根目录运行：

```powershell
python fix_schedule_task_refs.py
# 或指定配置导入目标
python fix_schedule_task_refs.py --config src.config:config
```

该工具与启动时自动校正**复用同一实现**
（`sync_schedule_task_indexes()`），打印被修正的任务数。

## 常见问题

- **统一为稳定标识**：无论历史格式是数字索引（`-t 1`）还是任务名
  （`-t 日常任务`），校正都会统一迁移为 `模块路径.类名` 形式，之后任务排序
  变化不再影响计划任务；运行时按类名回退匹配，模块移动也能自动修正。
- **其它应用任务**：`ok-*` 其它应用注册的任务不会被本应用改写。
