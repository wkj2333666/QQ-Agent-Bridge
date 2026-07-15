# Whisper 语音与视频处理设计

## 目标

为 QQ Agent Bridge 增加可靠的本地语音识别基础能力，并增强视频理解 skill，使语音和视频都以可验证资源的方式进入 agent 上下文。目标是：

- QQ 语音默认自动转写，像普通文字一样参与 ask、task 和闲聊上下文。
- 语音识别不依赖 Cursor、Codex 或其他 agent 是否主动调用工具。
- 保留原始音频路径，供 task 继续做转码、分析或发送。
- 引用语音与直接发送的语音使用同一条资源 enrich 链路。
- B 站及常见中文视频网站的总结必须基于字幕、音频转写、实际画面或页面正文等证据，不能根据标题猜测。
- 部署过程不修改系统 Python 环境，不给 task agent 放宽 workspace 外写权限。

## 非目标

- 本次不实现唱歌、歌声克隆或通用音乐生成。
- 本次不把视频下载器、OCR、字幕解析器硬编码进 bridge；这些能力由现有 agent 工具和视频 skill 协作完成。
- 本次不自动把超过 QQ 语音限制的音频发送为语音；发送规则继续由现有音频资源规范负责。

## 方案选择

采用 bridge 原生的 `whisper.cpp` runner，而不是把基础 ASR 委托给 agent 或新增 Python 推理环境。

原因：

1. 当前设备为 ARM CPU，whisper.cpp 的轻量二进制和量化模型更适合长期运行。
2. ASR 是资源预处理，不应依赖 agent 的提示词遵循和工具发现。
3. 单独 runner 可以设置并发、超时、模型路径和输出解析，失败行为可测试。
4. 现有 agent sandbox 仍只负责 workspace 内的任务文件，不需要为 ASR 放开额外目录。

初始部署使用已验证的 whisper.cpp Tiny Q8 模型作为运行基线；模型路径配置化，后续可替换为 Base 或其他兼容模型。默认解码保持 fallback 和稳定参数，不能为了速度关闭必要的 fallback。

## 架构

### 数据流

```text
QQ record
  -> OneBot/NapCat get_record(out_format=wav)
  -> workspace-local resource staging
  -> WhisperRunner (bounded subprocess, max concurrency 1)
  -> PreparedResource.transcript
  -> format_resource_context
  -> ask/task/chat/proactive agent prompt
```

### 模块边界

#### OneBot 录音转换

在现有 OneBot adapter 中增加小而明确的录音转换接口：

- 输入 NapCat/OneBot 的 `file` 或 `file_id`。
- 调用 `get_record`，传入 `out_format=wav`。
- 返回可供资源下载或读取的 WAV URL/路径及原始响应信息。
- 设置短超时；失败不阻塞整个消息处理。

不在 bridge 中实现 Silk 解码。NapCat 负责协议相关转换，bridge 只消费转换结果。

#### `WhisperRunner`

新增独立模块，职责仅为本地音频转写：

- 接收已落盘的音频路径。
- 使用 `asyncio.create_subprocess_exec` 调用配置的 whisper.cpp 二进制。
- 使用 semaphore 限制并发，默认 1。
- 使用 `asyncio.wait_for` 限制单条语音总耗时。
- 解析稳定的纯文本或 JSON 输出，清理空白和控制字符。
- 返回明确的成功文本或结构化失败原因。
- 不调用 agent，不访问 workspace 外文件。

配置至少包含：`enabled`、`binary`、`model`、`language`、`timeout_seconds`、`max_concurrent`、`cache_enabled`、`cache_root`。配置文件只保存路径和策略，不自动下载模型。

#### 资源 enrich

`ResourceManager.prepare` 在资源落盘后对 `voice` 做 enrich：

1. 如果资源已经是可读取的 WAV/音频文件，直接进入转写。
2. 如果只有 QQ Silk URL 或 file token，先请求 OneBot 转换接口，再保存 WAV。
3. 在不改变原始资源身份的前提下附加转写文本和识别状态。
4. 对引用消息中的 voice 资源复用同一逻辑。

`PreparedResource` 增加可选字段：转写文本、转写状态、语言和错误摘要。原始资源路径仍保留；若转换后的 WAV 路径不同，提示中同时标明资源用途，避免 agent 把“转写失败”误认为“没有语音”。

#### 上下文格式

语音上下文使用明确、不可伪造的标记，例如：

```text
- voice: downloads/.../voice.wav duration=12s
  transcript (verified by local Whisper, language=zh): 今天天气怎么样
```

失败时使用：

```text
- voice: downloads/.../voice.wav duration=12s
  transcript: unavailable (NapCat conversion failed: ...)
```

agent 必须把失败状态当作不可知，不得根据聊天历史补写语音内容。

## 配置与部署

新增 `whisper` 配置段，默认保持关闭或路径为空，避免开源用户启动后突然下载模型。当前部署配置显式开启并填写本机路径。

部署原则：

- whisper.cpp 二进制和模型放在 bridge 专用运行目录，不写入 agent task workspace。
- 不修改系统 Python、mamba base 或系统包。
- 模型文件由部署步骤显式下载并校验 SHA-256。
- bridge 以只读方式使用二进制和模型；缓存目录单独限制在配置指定位置。
- 默认 ASR 并发为 1，避免与 Cursor 任务争抢 CPU 和内存。
- 60 秒 QQ 语音作为默认输入上限；超过限制时转写仍可配置，但必须有明确的资源上限和超时。

推荐目录形态：

```text
~/.local/share/qq-agent-bridge/asr/
  bin/whisper-cli
  models/ggml-tiny-q8_0.bin
  cache/
```

该目录不纳入 Git。项目配置示例使用占位路径，个人配置再填写实际路径。

## 缓存策略

转写缓存以音频内容 SHA-256、模型文件标识、语言和 runner 版本共同作为 key，避免同一语音重复消耗 CPU。缓存：

- 只保存转写文本和必要元数据，不改变消息权限。
- 有大小和 TTL 限制。
- 失败结果不长期缓存，避免一次临时网络/API 错误永久污染结果。
- 缓存命中仍将原始资源路径交给 agent。

## 视频 skill 规则

增强 `skills/qq-agent-runtime/references/visual-media.md`：

1. 先解析短链为 canonical URL，读取页面元数据、简介、章节和可用字幕。
2. 有字幕时优先使用字幕，并记录字幕来源和覆盖范围。
3. 无可用字幕时，下载或获取可访问音频，使用本地 Whisper 或其他实际可用转写工具。
4. 需要视觉信息时按时间段抽帧并读取画面字幕、人物、场景和关键变化；不能只抽一张封面。
5. 搜索结果、标题、评论和相似文章只能作为背景，不能单独支撑“视频内容概括”。
6. 每个结论都应能回溯到页面、字幕、音频转写或画面证据；证据不足时输出阻塞点和不确定性。
7. 如果下载、登录、字幕、音频或抽帧失败，必须明确报告失败环节，不得生成看似完整的内容摘要。
8. 处理 B 站、`b23.tv` 等中文视频时保留中文编码、字幕和字体要求，必要时使用项目配置的 CJK 字体。
9. 任务过程可以发送简短进度消息，但不要把工具原始输出或无意义的“这一步完成了”直接转发给 QQ。

## 错误处理

错误按边界分类，不互相伪装：

- OneBot 转换失败：报告无法取得可解码音频。
- 文件下载失败：报告资源不可访问或超限。
- Whisper 超时：报告转写超时，并保留原始音频供 task 处理。
- 模型/二进制缺失：报告部署配置缺失，不声称已转写。
- 空转写：报告未识别到可靠文本，不以标题或上下文补全。
- 视频证据不足：只输出已验证元数据和阻塞点。

所有失败都应是软失败：不能因为一条语音或一个视频资源失败而丢弃同一消息中的文字内容或其他附件。

## 测试策略

### 单元测试

- Whisper 配置默认值、路径和非法值。
- runner 正确传参、解析输出、超时、空输出、非零退出码。
- semaphore 确保最大并发为 1。
- OneBot `get_record` 参数包含 `out_format=wav`。
- ResourceManager 对 Silk/file token 先转换、再落盘和转写。
- 直接语音和引用语音都把 transcript 放进上下文。
- 转写失败仍保留资源并输出明确错误。
- 缓存命中不重复调用 runner。
- 视频 skill 明确禁止标题猜测，并要求字幕/音频/抽帧证据。

### 集成 smoke test

使用 fake `whisper-cli` 可执行文件模拟成功、超时和失败，验证完整的消息资源链路，不依赖真实模型。真实部署后再运行一次短中文 WAV smoke test，记录耗时、输出和峰值内存，但不把私人音频或模型缓存提交到 Git。

## 分阶段交付

1. 部署 whisper.cpp runner、模型和 smoke test。
2. 接入 OneBot 录音转换及 ResourceManager 自动转写。
3. 将 transcript 接入 ask/task/闲聊上下文，补齐引用语音。
4. 更新视频 skill 和媒体相关测试。
5. 做完整测试、资源目录检查和部署环境检查，确认没有把模型、音频或个人配置纳入 Git。
