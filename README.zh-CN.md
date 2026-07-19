# QQ Agent Bridge

中文文档 | [English](README.md)

QQ Agent Bridge 是一个偏安全取向的 OneBot v11 桥接层，用来把 QQ 私聊/群聊接到本地 CLI Agent，比如 Cursor Agent、Codex、Claude Code，或你自己的命令行封装。

它不是 QQ 协议实现，也不是完整机器人平台。它只专注中间这一层：

```text
QQ 用户/群聊
  -> OneBot v11 网关，例如 NapCatQQ
  -> 反向 WebSocket
  -> bridge core：鉴权、路由、队列、记忆、资源暂存、脱敏
  -> 受限工作区里的本地 CLI Agent
  -> QQ 文本、图片、文件、音频或语音回复
```

## 重要提醒

本项目与腾讯、QQ、NapCatQQ、Cursor、OpenAI、Anthropic 等没有从属关系。

个人 QQ 网关可能违反平台规则，也可能带来账号风险。建议先用小号测试，并自行审查所使用的网关、模型、命令行工具和部署方式。你需要对账号、凭据、消息、文件、模型调用费用和合规风险负责。

## 功能

- OneBot v11 反向 WebSocket 服务。
- QQ 群聊 @ 路由，私聊默认触发。
- owner、私聊用户、群聊 allowlist。
- 普通用户可用只读命令：`/ask`、`/plan`、`/search`、`/task`、`/status`、`/help`、`/profile`、`/mode`。
- 持久化 `/schedule`；群内修改操作仅限 owner，允许的私聊用户可按配置管理自己的定时任务。
- owner 专用命令：`/code`、`/approve`、`/stop`、`/reset`、`/reload`。
- 任务队列和全局 agent 并发限制。
- 每个私聊/群聊独立的短期对话记忆。
- 每个群/私聊显式开启、严格隔离并由 SQLite 持久化的长期记忆。
- 群聊 ambient memory，让 bot 能理解最近群聊背景，但不把背景消息当命令执行。
- 每个群/用户可配置独立 profile，避免角色设定串群泄露。
- SQLite 持久化定时任务，支持单次、执行 N 次、限定时间范围和任意无限周期。
- 长任务进度消息和心跳。
- 附件缓存：手机端可先发图片/文件，再 @bot 处理最近附件。
- 支持把图片、文件、语音、音频、视频、URL、合并转发记录交给 agent。
- GIF、APNG 和动画 WebP 会在受限时长、帧数与分辨率内抽帧，连同原图交给 agent，避免只看首帧误判。
- 通过受保护的 `QQBOT_SEND_*` 指令发送 agent 生成的图片、文件、音频或 QQ 语音。
- 基于 Bubblewrap 的本地 CLI Agent 沙箱。
- Runtime skill pack：提示 agent 如何做搜索、查天气、处理办公文档、理解媒体、处理语音/音乐、避免幻觉，并遵守 QQ bridge 的资源收发协议。

## 当前状态

这是早期项目。Bridge 已经可用，但公开 API、配置格式和 runtime skill 格式在 `1.0` 之前都可能变化。

## 快速开始

```bash
git clone <repo-url> qq-agent-bridge
cd qq-agent-bridge

cp config.example.yaml config.yaml
# 编辑 config.yaml：
# - owners
# - allowed_users / allowed_groups
# - workspaces
# - agent.runtime 和对应运行时命令
# - onebot.access_token
# - QQ 网关登录后填写 bot.self_id

# 使用 uv 管理 bridge 环境，并由 uv.lock 锁定依赖版本。
# Agent 任务单独使用 micromamba base。
uv sync --locked

uv run python -m src.qq_agent_bridge.main --echo-only
```

先从允许的私聊发一条消息，确认 echo 模式能收到回复。OneBot 网关连接正常后，再运行正式模式：

```bash
uv run python -m src.qq_agent_bridge.main
```

## Agent 执行日志

如果需要排查 Agent 为什么慢、超时、重复调用工具或没有产出，可以在本地
`config.yaml` 的 `agent` 段开启 trace：

```yaml
agent:
  trace_enabled: true
  trace_root: "runtime/agent-traces"
  trace_max_bytes: 5242880
  trace_max_line_chars: 2000
```

每次 Agent 调用会写一个受限的 JSONL 文件，包含生命周期、工具摘要、标准错误、
超时和退出状态。日志默认关闭，不记录原始 prompt，也不会通过 QQ 发送；文件目录
为 `0700`、文件为 `0600`，并会对敏感字段和长度做脱敏/截断。排查结束后请按需清理
`runtime/agent-traces/` 中的本地日志。

## 自动存储维护

Bridge 内置了有边界的进程内清理，只管理三类由本项目创建的数据：

- `agent.sandbox_home` 下的 Agent 沙箱状态；
- `agent.trace_root` 直属目录中的普通 `*.jsonl` 文件；
- `resources.root` 下按日期保存的接收资源，以及 `outgoing`、`sending` 任务目录。

维护会在启动时先运行一次，之后按 `interval_seconds` 周期运行，默认值为
`21600`（6 小时）。每个任务结束后只做一次廉价的磁盘可用空间检查，必要时提前
请求压力清理。活动任务、主动发言、自然语言 schedule 解析、资源准备和 artifact
repair 与维护共用互斥门，因此清理不会和正在使用这些目录的工作重叠。

默认限制如下：

| 区域 | 容量上限 | 保留期 |
| --- | ---: | ---: |
| 沙箱 | `2147483648` 字节（2 GiB） | `1209600` 秒（14 天） |
| Trace | `536870912` 字节（512 MiB） | `1209600` 秒（14 天） |
| 接收资源 | 共用 `5368709120` 字节（5 GiB） | `604800` 秒（7 天） |
| `outgoing` / `sending` | 共用 `5368709120` 字节（5 GiB） | `86400` 秒（24 小时） |

相关文件系统可用空间低于 `5368709120` 字节（5 GiB）时会触发压力清理。扫描数量
和单次运行时间都有上限。符号链接、未知目录、Cursor 认证/配置、当前任务数据和
生成的 runtime skill bundle 不会被跟随或选中。失败日志不记录资源名、prompt 或
token，也不会阻止 bridge 继续运行。

设置 `storage_maintenance.enabled: false` 可以完全关闭自动维护。某区域的
`max_bytes: 0` 只关闭容量清理，保留期设为 `0` 只关闭对应的年龄清理。限制和周期
可以热更新；修改 `agent.sandbox_home`、`agent.trace_root`、默认 workspace 或
`resources.root` 后需要重启，当前进程在重启前仍只使用启动时验证过的旧根目录。

```yaml
storage_maintenance:
  enabled: false
```

## 作用域长期记忆

长期记忆用于持久保存偏好、稳定项目、反复出现的话题和群规范，但它采用显式开启
（opt-in）：即使全局功能可用，每个群和每个私聊作用域默认仍是关闭的。不同群、
不同私聊之间严格作用域隔离；群 A 不会读取群 B 的条目，私聊也不会读取其他用户
或群的条目。隔离由 bridge 和 SQLite 查询强制执行，不交给模型自行判断。

```yaml
commands:
  memory: user

long_term_memory:
  enabled: true
  default_scope_enabled: false
  groups: {}
  users: {}
  database_path: "data/long-term-memory.sqlite3"
  review:
    message_threshold: 40
    minimum_messages: 10
    idle_seconds: 600
    interval_seconds: 21600
    raw_ttl_seconds: 604800
    model: "auto"
    timeout_seconds: 90
    max_attempts: 3
  retrieval:
    max_items: 12
    max_chars: 1500
    minimum_score: 0.45
  decay:
    enabled: true
    interval_seconds: 86400
    grace_seconds: 2592000
    dormant_threshold: 0.40
```

当前作用域可用 `/memory enable` 和 `/memory disable` 开关。群聊中只有 owner 能
修改开关或执行 `/memory review now`；允许的私聊用户只能管理自己的私聊作用域。
常用管理命令包括 `/memory status`、`/memory remember <内容>`、`/memory list`、
`/memory show`、`/memory correct`、`/memory forget`，以及带二次确认的
`/memory clear`；完整写法见 `/memory help`。`/reset` 只清除最近聊天上下文，不会
删除长期记忆。

符合条件的用户文本在等待复盘期间最多保留 `604800` 秒（7 天）。达到消息数和
冷却阈值，或到达周期检查时会后台复盘。每日衰减在 `2592000` 秒宽限期后开始，
低分条目会先进入休眠，而不是继续被当作当前事实。显式 remember 也必须通过确定性
的秘密与敏感信息检查。原始文件、合并转发内容、bot 输出、控制命令、凭据以及
profile/system 内容不会进入采集。

复盘 curator 使用受限的 ask-only Agent：禁用网络、不能写项目工作区、没有常规
任务工具，也不会向 QQ 发送中间进度。每个模型建议都必须引用当前批次的来源行，
并通过规范化内容支持校验；curator JSON 任意层级的重复键都会被拒绝。受限 home 和
workspace 会在释放、App 关闭和启动失败时按所有权边界清理。数据库故障只会停用长期
记忆，不影响普通聊天、任务、schedule 或 OneBot 启动。

普通 Agent trace 会脱敏本次检索到的长期记忆条目，但不会改写最终发送到 QQ 的回答。
CQ 字符串中的真实 at 与 schedule 持久化的 mention 会保留结构化检索权限；纯展示文本
不会因此获得权限。

数据库是本地明文 SQLite。父目录权限为 `0700`，数据库文件为 `0600`，但运维者
仍需保护主机访问、磁盘快照和每一份备份。手动备份时应先停止 bridge，再同时复制
数据库及可能存在的 `-wal`、`-shm` 文件，或者使用 SQLite-aware backup 工具。
内置通用存储清理会保护这些持久化路径；等待复盘的原始行只受长期记忆自己的 TTL
策略管理。

`/reload` 可以热重载群/用户显式映射、复盘、检索和衰减设置；配置映射中没有出现
的作用域会保留此前通过 `/memory` 写入的选择。修改
`long_term_memory.database_path` 后必须重启，当前进程会继续使用已经打开的旧库。

## OneBot 网关

仓库里带了一个 NapCatQQ 的 compose 模板，位于 `runtime/napcat/`。它只是部署辅助；NapCatQQ 本身是独立项目。

典型启动方式：

```bash
cd runtime/napcat
mkdir -p data/qq config plugins
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose up
```

然后打开网关 WebUI，登录 QQ，并配置 WebSocket 客户端连接到 bridge：

```text
ws://127.0.0.1:8765/onebot
```

网关里的 access token 要和 `config.yaml` 里的 `onebot.access_token` 一致。

更多细节见 [runtime/napcat/README.md](runtime/napcat/README.md)。

## 常用命令

私聊：

```text
你好
/ask 解释这个报错
/task 搜索网页并整理摘要
```

群聊：

```text
@bot 你好
@bot /ask 解释一下
@bot /task 总结这个链接
```

普通命令：

- `/ask <文本>`：快速问答或轻量闲聊。
- `/plan <文本>`：只读规划，不修改文件。
- `/search <关键词>`：在配置工作区内做受限文本搜索。
- `/task <文本>`：显式执行较完整的 agent 任务，但不修改已有工作区文件。
- `/schedule <自然语言>`：创建持久化定时任务；时间由模型理解，再由 bridge 严格校验后保存。
- `/schedule help`：查看单次、有限次数、有限时间、无限周期和高级任意周期示例。
- `/schedule list|show|pause|resume|run|cancel`：用 ID、`0`/`1` 或 `-1`/`-2` 管理任务。
- `/status`：查看运行中和排队中的任务。
- `/help`：显示简短帮助。
- `/help <命令>` 或 `/<命令> help`：查看某个命令的详细用法、权限和示例。
- `/permission`：查看当前群的命令权限；`/permission set|clear` 仅群 owner 可修改，并会持久化。
- `/memory`：查看当前群或私聊这一精确作用域的长期记忆状态。
- `/memory help`：查看开启、复盘、记住、检查、修订、遗忘和清空的完整用法。
- `/profile`：查看当前 profile。
- `/profile set <提示词>`：设置当前群或当前私聊的 profile。
- `/profile clear`：清空当前群或当前私聊的 profile。
- `/mode`：查看本群无命令 @ 消息的默认模式。
- `/mode set chat|ask|plan|task`：设置本群默认模式；`chat` 先走闲聊判定，其余模式直接执行。
- `/mode clear`：清除本群覆盖，恢复全局默认模式。

每个命令都可以在 `config.yaml` 中单独配置权限级别：

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

`user` 表示所有已经通过用户/群权限校验的人，`owner` 表示仅 owner，
`disabled` 表示关闭命令。旧的布尔写法仍兼容：`true` 保留该命令历史上的默认权限，
`false` 等同于 `disabled`。

`commands.groups` 是群级覆盖，只对对应群生效；没有覆盖的命令继续继承全局设置。
群 owner 可以用 `/permission set <命令> user|owner|disabled` 修改，用
`/permission clear [命令]` 恢复全局设置。

owner 专用命令：

- `/code <请求>`：允许修改授权工作区，带确认流程。
- `/approve <job> <nonce>`：批准待确认任务。
- `/stop <job>`：取消任务。
- `/reset`：清空最近会话记忆和群聊背景，不影响长期记忆。
- `/reload`：热重载 `config.yaml`。

群聊里只有 owner 能修改群 profile 和 `/mode`；其他群成员可以查看。私聊里允许用户可以修改自己的私聊 profile，`/mode` 仅用于群聊。

定时任务方面，群聊创建和管理遵守当前群 `/schedule` 的有效权限：`owner` 只允许 owner 修改，`user` 允许已经通过群权限校验的成员修改；允许的私聊 user 可以管理自己的任务。
非 owner 创建定时任务时，时间解析和安全审查合并为一次模型调用；高频刷屏、资源消耗过大、
递归扩散或危险操作会在写入数据库前被拒绝。owner 创建的任务跳过这层额外安全审查。

无显式命令的 @ 消息如何处理由本群 `mode` 决定：`chat` 会先经过闲聊判定，模型决定需要回答时再进入 `ask`；`ask`、`plan`、`task` 则直接进入对应模式，跳过这次判定。无论 mode 如何设置，不 @ 我的普通群聊仍会进入群聊记忆和主动插话流程。修改会写回 `config.yaml`，重启后保留。

群聊里能否创建、暂停、恢复、立即执行或取消定时任务，由当前群 `/schedule`
权限决定；开启 `scheduler.allow_private_users` 后，允许的私聊用户可以管理自己的定时任务。
开源示例默认关闭 scheduler，请先检查 `config.example.yaml` 里的时区和限制再开启。
任务保存在 SQLite 中，bridge 重启后会继续调度。所有周期统一表示为 RFC 5545
RRULE，因此“工作日”“每两周周二”“每月最后一个工作日”等规则不需要写死新的周期类型。
调度限制支持 `/reload` 热更新；修改 `scheduler.database_path` 后需要重启 bridge。

示例：

```text
/schedule 明天早上十点提醒我喝水 噔噔噔
/schedule 每天早上八点告诉我北京市天气
/schedule 每月最后一个工作日下午六点整理本月工作
/schedule every 2h count 5 -- task 检查服务状态
/schedule every 1h forever -- task 检查服务状态
```

## Profile

Profile 是可选的角色/口吻提示词，写在 `config.yaml`：

```yaml
profiles:
  default: |
    - 你是一个轻量、友好、懂代码的 QQ 助手。
  groups:
    "2000000001": |
      - 你是这个群里的技术助手。
      - 回复短一点，优先给可执行建议。
  users:
    "1000000001": |
      - 你是私聊里的学习搭子。
      - 解释思路要耐心，但不要替用户做未授权决定。
```

隔离规则：

- 群聊使用 `profiles.groups[group_id]`，没有则使用 `profiles.default`。
- 私聊使用 `profiles.users[user_id]`，没有则使用 `profiles.default`。
- 其他群/用户的 profile 不会暴露给当前 agent。

群聊默认模式也可以直接写在配置里：

```yaml
mention_modes:
  default: chat
  groups:
    "2000000001": task
```

设置为 `chat` 时，@我会先判断是不是适合插话；设置为 `ask`、`plan` 或 `task` 时，
会直接进入对应模式。无论是哪种模式，不 @ 我的普通群聊仍会进入群聊记忆和主动插话流程。

只支持 `chat`、`ask`、`plan`、`task`；`ask`、`plan`、`task` 对应命令必须同时在
`commands` 中启用，`chat` 使用 `/ask` 的权限进行闲聊判定。`code` 和 `shell` 不能设为隐式默认模式。

## Agent Runtime

开源默认不启用任何 CLI Agent。你必须在 `config.yaml` 里显式选择运行时。

内置运行时：

- `cursor-cli`：按内置方式调用 Cursor Agent。
- `custom-cli`：使用 `agent.command` 命令模板，适合 Codex、Claude Code、wrapper script 或其他兼容 runner。

Cursor Agent 示例：

```yaml
agent:
  runtime: "cursor-cli"
  binary: "cursor-agent"
```

其他 CLI 示例：

```yaml
agent:
  runtime: "custom-cli"
  command:
    ask: ["your-agent", "--mode", "{mode}", "--model", "{model}", "{prompt}"]
    plan: ["your-agent", "--mode", "{mode}", "--model", "{model}", "{prompt}"]
    task: ["your-agent", "--workspace", "{workspace}", "{prompt}"]
    code: ["your-agent", "--workspace", "{workspace}", "{prompt}"]
```

支持的占位符：

- `{prompt}`：bridge 构造后的完整提示词。
- `{workspace}`：配置中的工作区路径。
- `{mode}`：`ask`、`plan`、`task` 或 `code`。
- `{model}`：当前命令选择的模型名，可能为空。
- `{stream}`：是否开启流式进度，值为 `true` 或 `false`。

Codex 和 Claude Code 的 CLI 参数不会被写死在项目里，因为这些 CLI 变化较快。建议把具体参数放在 `config.yaml` 或一个 wrapper script 里。

Agent 进程统一要求通过已有的 `micromamba run -n base` 环境启动。不要关闭
这个保护，也不要让 Agent 在 workspace 中创建 `.venv`、`venv`、`env` 或自行
安装依赖。如果 base 环境缺少依赖，应先报告缺失项，在 workspace 外准备好
环境后再重试。

Bubblewrap 网络访问通过 `agent.share_network` 显式开启。离线任务保持
`false`；只有需要联网搜索等能力时才设置为 `true`。

```yaml
agent:
  env_runner: "micromamba"
  env_name: "base"
  require_env: true
```

关键安全约束：

- `/ask` 和 `/plan` 保持只读。
- `/task` 可以使用工具，但只能在本次任务 outbox 里创建交付物。
- `/code` 是 owner 批准后的工作区编辑入口，未测试前建议保持关闭。
- 工作区只来自本地配置，不接受聊天消息里的路径作为可信输入。
- Bubblewrap 会把系统和运行时路径只读挂载，并使用私有 sandbox home 存放 agent 认证状态。

## 输入资源与动图

Bridge 会把 QQ 附件暂存到 workspace 内，再把相对路径交给 agent。启用
`resources.animation_enabled` 后，GIF、APNG 和动画 WebP 会先由 `ffprobe`
确认帧数与时长，再由 `ffmpeg` 有界抽取采样帧；当前 ffmpeg 无法解码时会使用
独立的 Pillow 子进程后备。默认最多 8 帧、前 30 秒、
最长边 1024 像素，源画布最多 4000 万像素；这些边界可在
`resources.animation_*` 中调整。部署机器需要能
找到配置的 `ffprobe` 和 `ffmpeg`，工具缺失或解析失败时原附件仍可用，但提示会
明确标记动态证据不可用。采样帧在本次 agent 调用结束、失败或超时后自动清理。

## 资源发送

如果 agent 生成了文件，需要在提示词指定的 per-job outbox 中创建文件，然后输出一行资源发送指令：

```text
QQBOT_SEND_IMAGE: <token> downloads/qq-agent-bridge/outgoing/<job>/image.png
QQBOT_SEND_FILE: <token> downloads/qq-agent-bridge/outgoing/<job>/report.pdf
QQBOT_SEND_AUDIO: <token> downloads/qq-agent-bridge/outgoing/<job>/audio.mp3
QQBOT_SEND_VOICE: <token> downloads/qq-agent-bridge/outgoing/<job>/voice.wav duration=12
```

Bridge 会从最终回复中移除这些指令，校验 token、路径和文件大小，然后通过 OneBot 发送。

QQ 语音只适合生成的人声短语音。`QQBOT_SEND_VOICE` 必须提供真实 `duration=`，并且实际时长不能超过 60 秒。泛音频、音乐、播客、长音频或无法验证时长的音频应当作为文件发送。

## 安全边界

- 默认 deny 配置。
- 用户、群、工作区、命令 allowlist。
- 群聊必须真实 @bot。
- 私聊用户必须在 allowlist 中。
- 允许群内普通成员使用只读命令。
- 高风险命令 owner-only。
- `/code` 带 nonce 确认流程。
- 任务超时、输出长度上限、消息去重。
- 常见 token、key、password 脱敏。
- 防止 agent 把内部提示词/上下文回显到 QQ。
- 暂存资源不会进入 `/search` 和对话记忆。
- 输出资源只能来自当前任务 outbox。
- 运行时状态和本地凭据默认被 git 忽略。

## 和已有项目的区别

- NapCatQQ、Lagrange 是 QQ 网关/协议实现；本项目消费 OneBot 事件，不实现 QQ 协议。
- OneBot v11 是协议契约；本项目是构建在它之上的一个 Python bridge。
- NoneBot2、Koishi 是通用机器人框架，插件生态更广；本项目更窄，重点是“QQ 到本地 CLI Agent”的安全桥接。
- LangBot、CowAgent 类项目更像多平台 AI bot 系统；本项目面向能读工作区、跑任务、生成交付文件的本地 CLI Agent，并用本地策略控制它们能做什么。

## 验证

```bash
uv run python dry_run.py
uv run pytest -q
git diff --check
```

默认测试不会真实调用 Cursor、Codex 或 Claude CLI。如果要在自己的机器上做 smoke test：

```bash
QQ_AGENT_BRIDGE_CLI_SMOKE=1 uv run pytest tests/test_cli_smoke.py -q
```

也可以用环境变量覆盖 smoke test 命令：

- `QQ_AGENT_BRIDGE_SMOKE_CURSOR_CMD`
- `QQ_AGENT_BRIDGE_SMOKE_CODEX_CMD`
- `QQ_AGENT_BRIDGE_SMOKE_CLAUDE_CMD`

如果要真实调用 agent runtime 跑较慢的 contract E2E：

```bash
QQ_AGENT_BRIDGE_AGENT_E2E=1 \
QQ_AGENT_BRIDGE_E2E_RUNTIME=cursor-cli \
QQ_AGENT_BRIDGE_E2E_CHAT_MODEL=auto \
QQ_AGENT_BRIDGE_E2E_TASK_MODEL=kimi-k2.5 \
uv run pytest tests/test_agent_e2e.py -q
```

`cursor-cli` 的真实 E2E 默认使用 `bwrap`，这样 Cursor 可以信任 pytest 创建的
临时 workspace，不需要交互确认。只有当你的 wrapper 已经自行处理 workspace trust
时，才设置 `QQ_AGENT_BRIDGE_E2E_BWRAP=0`。

Codex、Claude Code 或自定义 wrapper 可以使用 `custom-cli`：
设置 `QQ_AGENT_BRIDGE_E2E_RUNTIME=custom-cli`，并提供
`QQ_AGENT_BRIDGE_E2E_ASK_CMD`、`QQ_AGENT_BRIDGE_E2E_TASK_CMD` 等命令模板。
模板支持 `{prompt}`、`{workspace}`、`{mode}`、`{model}`、`{stream}`。

如果要跑语义层面的能力评测，可以启用下面这个 opt-in 测试。它会先调用目标
agent 执行任务，再调用 judge agent 评判是否满足能力标准；同时仍会做确定性的
硬检查，比如必须包含 token、不能出现禁止措辞等。

```bash
QQ_AGENT_BRIDGE_CAPABILITY_EVAL=1 \
QQ_AGENT_BRIDGE_E2E_RUNTIME=cursor-cli \
QQ_AGENT_BRIDGE_CAPABILITY_CHAT_MODEL=auto \
QQ_AGENT_BRIDGE_CAPABILITY_TASK_MODEL=kimi-k2.5 \
uv run pytest tests/test_agent_capability_eval.py -q
```

默认 judge 复用同一个 runtime。需要独立裁判时，可以设置
`QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_RUNTIME`、
`QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_MODEL` 或
`QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_ASK_CMD`。

## 仓库卫生

不要提交真实运行状态或敏感信息：

- `config.yaml`
- `.env`
- 二维码、cookie、QQ 凭据、登录截图
- OneBot/NapCat token
- Cursor/Codex/Claude auth state
- `runtime/*/data/`
- `runtime/*/config/`
- `runtime/*/plugins/`
- `workspace/downloads/`
- 私聊/群聊日志

公开仓库前请阅读 [SECURITY.md](SECURITY.md) 和 [docs/PUBLISHING.md](docs/PUBLISHING.md)。

## License

MIT License. See [LICENSE](LICENSE).
