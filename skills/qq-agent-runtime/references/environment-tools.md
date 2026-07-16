# 运行环境与工具手册

这份手册是任务型 Agent 使用本机工具前的操作边界。先读它，再按需探测；不要一次性安装依赖或扫描整个文件系统。

## 两层环境

- QQ bridge 自己运行在项目 `.venv` 中，由 `uv run` 管理；这是 bridge 的依赖环境，不是任务 Agent 的工作环境。
- 任务 Agent 运行在 `micromamba` 的 `base` 环境中。所有 Python 库探测、脚本执行和库版本确认都必须使用：

```bash
micromamba run -n base python -c '...'
```

- 不要用裸 `python3`、`python`、`pip`、`uv` 或 `.venv/bin/python` 判断任务环境；它们可能指向 bridge 环境或沙箱中的另一个解释器。
- 不要创建 venv、安装 pip 包、创建 conda/mamba 环境，也不要为了单个任务修改系统环境。缺依赖时报告阻塞，不要假装已经具备能力。

## 安全探测顺序

先做无副作用、针对性的探测，不要用大范围 `find /`：

```bash
command -v chromium ffmpeg curl node
command -v pandoc libreoffice yt-dlp wkhtmltopdf
micromamba run -n base python -c 'import fitz; print(fitz.__doc__)'
micromamba run -n base python -c 'import yaml, aiohttp, websockets; print("base libraries ok")'
```

导入测试的退出码和输出才是能力证据；`which` 找到一个命令不等于它能完成当前任务。需要探测其他库时，只导入当前流水线真正需要的库。

## 当前部署的能力基线

这是本项目当前部署的已验证基线，不代表所有用户机器都相同，仍需先探测：

- base 已有 PyMuPDF（导入名 `fitz`），可用于读取、检查和程序化生成 PDF。
- base 已有 `yaml`、`aiohttp`、`websockets` 等 bridge/资源处理依赖。
- 当前 base 没有把 `openpyxl`、`pandas`、`python-docx`、`python-pptx`、`reportlab`、`weasyprint` 当作可用前提；使用前必须导入探测。
- 系统工具中可优先探测 Chromium、ffmpeg、curl、Node；不要假定 `yt-dlp`、Pandoc、LibreOffice、ImageMagick 或 wkhtmltopdf 存在。

## 推荐流水线

### PDF

1. HTML/CSS 转 PDF：先 `command -v chromium`，使用现有 Chromium 的 headless 模式和 outbox 下的临时用户目录；不要下载或安装浏览器。
2. 程序化 PDF、PDF 读取或结果校验：先确认 `fitz`，用 `micromamba run -n base python` 执行 PyMuPDF。
3. 生成后必须验证文件存在、非空，并能被 PyMuPDF 打开、读取页数；验证失败不能声称 PDF 已完成。
4. 不要先盲目轮询 reportlab、fpdf、weasyprint 等库。它们不是本项目的默认依赖；缺失时按上述两条已知路径选择，仍不可行就报告阻塞。

示例校验：

```bash
test -s "$OUTBOX/report.pdf"
micromamba run -n base python -c \
  'import fitz, sys; doc=fitz.open(sys.argv[1]); print("pages", doc.page_count)' \
  "$OUTBOX/report.pdf"
```

### Excel、Word、PPT

- 先按需导入探测 `openpyxl`、`python-docx`、`python-pptx`；库不存在时不要安装。
- Excel 无法可靠生成 xlsx 时，按办公文档手册降级为 CSV，并明确告诉用户格式变化。
- 每个产物都要做存在、非空和可读取验证；验证通过后才输出 QQ 发送指令。

### 图片、视频、音频

- 图片/视频转码和抽帧优先使用已探测到的 ffmpeg；先检查输入和输出，不要只凭命令返回“看起来成功”。
- 视频下载、网页访问、字幕或转写必须遵守对应媒体 skill；工具缺失、登录或反爬阻塞时报告证据，不要用标题臆测内容。
- 音频识别、TTS 和转换使用项目已有的 backend/config；普通转码不是语音识别或唱歌能力。

## 结果与交付

- 工具运行期间只报告真实完成的阶段，使用 `QQBOT_PROGRESS`；不要把 shell 命令、内部路径或探测过程原样发给用户。
- 产物必须写在当前任务 outbox，并用 QQ bridge interface 中的 `QQBOT_SEND_*` 指令发送。
- 任何“工具不存在”“导入失败”“文件为空”“验证失败”都必须保留为阻塞证据，不能把替代方案的尝试写成成功。
