# 群级命令权限与命令帮助设计

## 目标

为每个允许的 QQ 群提供独立的命令权限覆盖，并持久化到 `config.yaml`；同时让每个命令都能通过 `help` 子命令获得适合 QQ 阅读的用法说明，减少用户因为权限、参数或群聊限制而反复试错。

## 权限配置模型

保留现有全局 `commands` 配置作为默认值，在其下增加 `groups` 映射：

```yaml
commands:
  ask: user
  task: user
  code: owner
  shell: disabled
  permission: user
  groups:
    "180188783":
      task: disabled
      search: owner
```

`commands.groups` 不是一个命令，不参与命令枚举。全局命令配置继续兼容旧的布尔写法：`true` 按历史默认权限解析，`false` 解析为 `disabled`。群级值只接受 `user`、`owner`、`disabled`；非法值在加载时拒绝，避免静默放宽权限。

权限解析规则：

1. 私聊只使用全局命令权限，不读取群级覆盖。
2. 群聊先读取本群覆盖；某个命令没有覆盖时回退到全局权限。
3. 群级覆盖只改变该群的授权级别，不改变 allowlist、workspace、确认流程或命令本身的业务限制。
4. `Policy.allow()` 是唯一运行时授权入口，统一调用带群号的有效权限解析，防止某个命令处理器绕过覆盖。
5. `/mode set ask|plan|task` 检查当前群的有效权限，不能通过全局开启来绕过本群禁用。

## 权限命令

新增 `/permission` 命令。它默认对已授权用户可见，但只有 owner 能执行修改操作：

```text
/permission
/permission set <command> user|owner|disabled
/permission clear [command]
/permission help
```

行为：

- `/permission` 显示当前群所有已知命令的全局权限、本群覆盖和最终有效权限；私聊显示全局权限并说明群级覆盖仅适用于群聊。
- `/permission set` 只允许群 owner 修改当前群；私聊返回“仅用于群聊”。
- `/permission clear <command>` 删除一个覆盖并回退到全局；不带命令时清除本群全部覆盖。
- 修改后通过新的 `command_access_store.py` 使用现有原子 top-level YAML 写入器持久化到 `commands` 块，失败时恢复内存状态并返回明确错误。
- `permission` 本身的全局默认值为 `user`；修改权限仍由业务处理器单独检查 owner，因此普通用户可以查看但不能修改。
- `/permission` 必须经过正常命令权限和群/用户 allowlist 检查；全局禁用时整条命令不可用。

## 命令帮助模型

新增结构化命令帮助表，覆盖 `ask`、`plan`、`search`、`task`、`code`、`status`、`stop`、`approve`、`shell`、`help`、`profile`、`mode`、`reset`、`reload`、`schedule` 和 `permission`。

支持两种入口：

```text
/help <command>
/<command> help
/<command> 帮助
```

帮助内容至少包含：用途、语法、所需权限、群/私聊限制、确认或附件要求（如适用）和一个简短示例。`/help` 无参数仍保持现有简短、按角色过滤的概览。

帮助请求在创建 Agent job 前被 bridge 拦截，因此不会消耗 Agent 并发额度，也不会被误当成普通任务。`/<command> help` 仍先经过该命令本身的授权检查；`/help <command>` 经过 `help` 的授权，并对目标命令显示其当前状态。禁用命令的帮助可以说明“当前已禁用”，但不会因此启用或执行命令。

现有 `/schedule help` 的详细时区、自然语言和结构化示例保持原输出能力；新入口 `/help schedule` 复用同一套核心说明，避免两个入口长期漂移。

## 文件边界

- `src/qq_agent_bridge/config.py`：增加群级命令覆盖加载、校验和有效权限解析。
- `src/qq_agent_bridge/command_access_store.py`：格式化并持久化 `commands` top-level block。
- `src/qq_agent_bridge/policy.py`、`types.py`：注册 `permission` 并让统一授权入口传递群上下文。
- `src/qq_agent_bridge/command_help.py`：集中维护命令帮助元数据和渲染逻辑。
- `src/qq_agent_bridge/self_knowledge.py`：让概览和 Agent 自我说明使用群级有效权限。
- `src/qq_agent_bridge/main.py`：拦截帮助入口，实现 `/permission` 读写和持久化回滚。
- `config.example.yaml`、README：补充配置和交互示例。

## 测试与验收

必须覆盖：

1. 全局权限加载和旧布尔配置保持兼容。
2. 群级覆盖生效、缺省回退、清除回退、非法值拒绝，且私聊不读取覆盖。
3. 非 owner 不能 set/clear，owner 能修改；原子写入失败不会留下错误的内存状态。
4. `/permission` 读写路径经过 allowlist 和命令权限检查。
5. `/help` 概览保持现有按角色过滤；`/help task` 和 `/task help` 均返回详细说明。
6. 帮助请求不会创建 job；`/schedule help` 的既有示例测试继续通过。
7. 所有命令帮助都包含语法和权限信息，未知命令返回友好提示而不是启动 Agent。
8. 完整 pytest、compileall 和 `git diff --check` 通过。
