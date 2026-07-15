# Configurable CJK Font Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make QQ task agents reliably select and verify an operator-configured Chinese font when generating images and PDFs, without installing fonts or widening filesystem write access.

**Architecture:** A typed RenderingConfig is serialized into a minimal workspace-local runtime manifest. A dependency-free skill helper resolves configured fonts, using fontconfig discovery only when no explicit paths exist. Bubblewrap exposes fontconfig metadata read-only, while the runtime skill teaches renderers to select, embed, and verify the resolved font.

**Tech Stack:** Python 3.11+, dataclasses, PyYAML, fontconfig CLI, Bubblewrap, pytest, optional PyMuPDF in micromamba base.

## Global Constraints

- Do not download, install, copy, edit, or bundle font binaries.
- Explicit configured paths take precedence; a non-empty invalid list never falls back to discovery.
- Discovery runs only when cjk_font_files is empty and auto_detect_cjk_font is true.
- Never auto-bind arbitrary configured host paths.
- Mount /etc/fonts and /var/cache/fontconfig read-only when present.
- Preserve unrelated dirty work, especially the existing /mode implementation.

## File Map

- src/qq_agent_bridge/config.py: typed configuration and strict YAML loading.
- src/qq_agent_bridge/runtime_skill.py: copy scripts and atomically write a minimal manifest.
- src/qq_agent_bridge/main.py: pass current rendering configuration to future task bundles.
- src/qq_agent_bridge/cursor_adapter.py: read-only fontconfig mounts.
- skills/qq-agent-runtime/scripts/resolve_cjk_font.py: deterministic resolver CLI.
- skills/qq-agent-runtime/references/cjk-rendering.md: renderer-independent workflow.
- skills/qq-agent-runtime/SKILL.md and media references: progressive-disclosure routing.
- tests/test_cjk_font_resolver.py: isolated resolver tests.
- tests/helpers/cjk_render_smoke.py and tests/test_cjk_rendering_smoke.py: opt-in real rendering.
- config.example.yaml, README.md, README.zh-CN.md: operator documentation.

---

### Task 1: Typed Rendering Configuration

**Files:**
- Modify: src/qq_agent_bridge/config.py
- Modify: config.example.yaml
- Modify: tests/test_config.py

**Interfaces:**
- Produces RenderingConfig(cjk_font_files: list[str], auto_detect_cjk_font: bool).
- Produces BridgeConfig.rendering: RenderingConfig.

- [ ] **Step 1: Write failing tests**

Add:

~~~python
def test_config_loads_ordered_cjk_font_sources(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("""
rendering:
  cjk_font_files:
    - /fonts/SourceHanSansSC-Regular.otf
    - /fonts/wqy-zenhei.ttc
  auto_detect_cjk_font: false
""", encoding="utf-8")

    cfg = BridgeConfig.load(path)

    assert cfg.rendering.cjk_font_files == [
        "/fonts/SourceHanSansSC-Regular.otf",
        "/fonts/wqy-zenhei.ttc",
    ]
    assert not cfg.rendering.auto_detect_cjk_font


@pytest.mark.parametrize("rendering", [
    {"cjk_font_files": "/fonts/a.ttf"},
    {"cjk_font_files": [1]},
    {"cjk_font_files": [""]},
    {"auto_detect_cjk_font": "yes"},
])
def test_config_rejects_invalid_cjk_font_settings(
    tmp_path: Path,
    rendering: object,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"rendering": rendering}), encoding="utf-8")
    with pytest.raises(ValueError, match="rendering"):
        BridgeConfig.load(path)
~~~

Also assert the example config defaults to an empty list with discovery enabled.

- [ ] **Step 2: Verify RED**

Run: .venv/bin/pytest tests/test_config.py -q

Expected: RenderingConfig/BridgeConfig.rendering assertions fail.

- [ ] **Step 3: Implement strict loading**

Add:

~~~python
@dataclass
class RenderingConfig:
    cjk_font_files: list[str] = field(default_factory=list)
    auto_detect_cjk_font: bool = True


def _load_rendering(raw: Any) -> RenderingConfig:
    if raw is None:
        return RenderingConfig()
    if not isinstance(raw, dict):
        raise ValueError("rendering must be a mapping")
    files = raw.get("cjk_font_files", [])
    if not isinstance(files, list) or any(
        not isinstance(item, str) or not item.strip() for item in files
    ):
        raise ValueError(
            "rendering.cjk_font_files must be a list of non-empty strings"
        )
    auto = raw.get("auto_detect_cjk_font", True)
    if not isinstance(auto, bool):
        raise ValueError("rendering.auto_detect_cjk_font must be a boolean")
    return RenderingConfig(
        cjk_font_files=[item.strip() for item in files],
        auto_detect_cjk_font=auto,
    )
~~~

Wire it into BridgeConfig.load. Add this example:

~~~yaml
rendering:
  cjk_font_files: []
  auto_detect_cjk_font: true
  # /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
  # /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc
~~~

- [ ] **Step 4: Verify GREEN**

Run: .venv/bin/pytest tests/test_config.py -q

Expected: all tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/qq_agent_bridge/config.py config.example.yaml tests/test_config.py
git commit -m "feat: configure CJK rendering fonts"
~~~

---

### Task 2: Deterministic Font Resolver

**Files:**
- Create: skills/qq-agent-runtime/scripts/resolve_cjk_font.py
- Create: tests/test_cjk_font_resolver.py

**Interfaces:**
- Produces resolve_font(config_path: Path) -> ResolvedFont.
- CLI prints {"path": str, "family": str, "source": "configured"|"fontconfig"}.
- CLI exits 2 with a concise diagnostic on failure.

- [ ] **Step 1: Write failing resolver tests**

Load the script with importlib.util.spec_from_file_location. Test:

~~~python
def test_explicit_font_wins_and_does_not_run_fc_list(tmp_path: Path) -> None:
    first = tmp_path / "first.ttc"
    first.write_bytes(b"font")
    config = write_manifest(tmp_path, [str(first)], auto=True)
    calls: list[list[str]] = []

    result = MODULE.resolve_font(
        config,
        which=fake_which,
        run=fake_run(calls, family="WenQuanYi Zen Hei", langs="zh-cn|zh-sg"),
    )

    assert result.path == str(first)
    assert result.source == "configured"
    assert all("fc-list" not in call[0] for call in calls)


def test_invalid_explicit_font_never_falls_back(tmp_path: Path) -> None:
    config = write_manifest(tmp_path, [str(tmp_path / "missing.ttf")], auto=True)
    with pytest.raises(MODULE.FontResolutionError, match="configured"):
        MODULE.resolve_font(config, which=fake_which, run=unexpected_run)


def test_discovery_has_stable_family_preference(tmp_path: Path) -> None:
    config = write_manifest(tmp_path, [], auto=True)
    result = MODULE.resolve_font(
        config,
        which=fake_which,
        run=ranked_fontconfig_run(tmp_path),
    )
    assert result.family == "Noto Sans CJK SC"
    assert result.source == "fontconfig"


def test_disabled_discovery_without_font_is_failure(tmp_path: Path) -> None:
    config = write_manifest(tmp_path, [], auto=False)
    with pytest.raises(MODULE.FontResolutionError, match="disabled"):
        MODULE.resolve_font(config, which=fake_which, run=unexpected_run)
~~~

Also test missing fc-query, no zh language tag, malformed JSON, directories, and CLI failure output.

- [ ] **Step 2: Verify RED**

Run: .venv/bin/pytest tests/test_cjk_font_resolver.py -q

Expected: import failure because the script does not exist.

- [ ] **Step 3: Implement minimal resolver**

Use:

~~~python
@dataclass(frozen=True)
class ResolvedFont:
    path: str
    family: str
    source: str


def resolve_font(
    config_path: Path,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ResolvedFont:
    files, auto = _load_manifest(config_path)
    if files:
        failures: list[str] = []
        for raw in files:
            try:
                return _validate_candidate(Path(raw), "configured", which, run)
            except FontResolutionError as exc:
                failures.append(str(exc))
        raise FontResolutionError(
            "no configured CJK font is usable: " + "; ".join(failures)
        )
    if not auto:
        raise FontResolutionError(
            "CJK font discovery is disabled and no font is configured"
        )
    return _discover_fontconfig(which, run)
~~~

_validate_candidate requires a readable regular file, executes fc-query with argument lists and no shell, and accepts only a record containing zh or zh-* in its language tags. _discover_fontconfig executes:

~~~text
fc-list :lang=zh -f %{file}\\t%{family}\\n
~~~

Deduplicate by path, then sort by family preference: Noto CJK, Source Han/思源, WenQuanYi/文泉驿, Unifont, other; path is the stable tie-breaker. Validate ranked candidates. Serialize dataclasses.asdict with ensure_ascii=False.

- [ ] **Step 4: Verify GREEN and host behavior**

Run:

~~~bash
.venv/bin/pytest tests/test_cjk_font_resolver.py -q
python skills/qq-agent-runtime/scripts/resolve_cjk_font.py --config /tmp/cjk-rendering-config.json
~~~

The temporary JSON contains the local WenQuanYi path. Expected output names WenQuanYi Zen Hei and source configured.

- [ ] **Step 5: Commit**

~~~bash
git add skills/qq-agent-runtime/scripts/resolve_cjk_font.py tests/test_cjk_font_resolver.py
git commit -m "feat: resolve configured CJK fonts"
~~~

---

### Task 3: Runtime Bundle Delivery

**Files:**
- Modify: src/qq_agent_bridge/runtime_skill.py
- Modify: src/qq_agent_bridge/main.py
- Modify: tests/test_runtime_skill.py
- Modify: tests/test_app_async.py

**Interfaces:**
- Changes prepare_runtime_skill_bundle(workspace, resource_root, rendering=None) -> str.
- Return value remains the workspace-relative references directory.
- Bundle gains scripts/resolve_cjk_font.py and rendering-config.json.

- [ ] **Step 1: Write failing tests**

~~~python
rendering = RenderingConfig(
    cjk_font_files=["/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"],
    auto_detect_cjk_font=False,
)
reference_base = prepare_runtime_skill_bundle(
    workspace,
    "downloads/qq-agent-bridge",
    rendering,
)
bundle = workspace / Path(reference_base).parent
assert json.loads((bundle / "rendering-config.json").read_text()) == {
    "cjk_font_files": rendering.cjk_font_files,
    "auto_detect_cjk_font": False,
}
assert (bundle / "scripts" / "resolve_cjk_font.py").is_file()
~~~

Assert the task prompt contains copied reference, resolver, and manifest paths. Spy in an app test and prove cfg.rendering is passed for a future job after /reload.

- [ ] **Step 2: Verify RED**

Run: .venv/bin/pytest tests/test_runtime_skill.py tests/test_app_async.py -q

Expected: unsupported third argument and missing files.

- [ ] **Step 3: Implement bundle changes**

Copy scripts/*.py. Atomically write only:

~~~python
payload = {
    "cjk_font_files": list(rendering.cjk_font_files),
    "auto_detect_cjk_font": rendering.auto_detect_cjk_font,
}
~~~

Use NamedTemporaryFile in the bundle directory, flush it, then os.replace. Generalize path rewriting from the references prefix to the runtime skill root so both references and scripts become workspace-relative. Pass self.cfg.rendering in App._prepare_runtime_skill_bundle.

- [ ] **Step 4: Verify GREEN**

Run: .venv/bin/pytest tests/test_runtime_skill.py tests/test_app_async.py -q

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add src/qq_agent_bridge/runtime_skill.py src/qq_agent_bridge/main.py tests/test_runtime_skill.py tests/test_app_async.py
git commit -m "feat: deliver CJK font settings to tasks"
~~~

---

### Task 4: Read-Only Fontconfig Sandbox Access

**Files:**
- Modify: src/qq_agent_bridge/cursor_adapter.py
- Modify: tests/test_cursor_adapter.py

**Interfaces:**
- Changes only _system_ro_binds().
- Does not add configured arbitrary paths or writable mounts.

- [ ] **Step 1: Write failing test**

~~~python
def test_bwrap_exposes_existing_fontconfig_paths_read_only() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    cmd = adapter._build_cmd("hello", "/tmp", "task", model=None)

    for path in ("/etc/fonts", "/var/cache/fontconfig"):
        if Path(path).exists():
            assert _has_bind(cmd, "--ro-bind", path, path)
            assert not _has_bind(cmd, "--bind", path, path)
~~~

Also configure /opt/private-font.ttf and prove it is absent from the bwrap command.

- [ ] **Step 2: Verify RED**

Run: .venv/bin/pytest tests/test_cursor_adapter.py -q

Expected: /etc/fonts assertion fails on this host.

- [ ] **Step 3: Implement minimal mount change**

Append /etc/fonts and /var/cache/fontconfig to the existing conditional candidates. Do not read cfg.rendering in the adapter.

- [ ] **Step 4: Verify GREEN and commit**

~~~bash
.venv/bin/pytest tests/test_cursor_adapter.py -q
git add src/qq_agent_bridge/cursor_adapter.py tests/test_cursor_adapter.py
git commit -m "fix: expose fontconfig read only in agent sandbox"
~~~

---

### Task 5: Skill Rules, Docs, And Real Rendering Smoke Test

**Files:**
- Create: skills/qq-agent-runtime/references/cjk-rendering.md
- Modify: skills/qq-agent-runtime/SKILL.md
- Modify: skills/qq-agent-runtime/references/office-documents.md
- Modify: skills/qq-agent-runtime/references/visual-media.md
- Create: tests/helpers/cjk_render_smoke.py
- Create: tests/test_cjk_rendering_smoke.py
- Modify: tests/test_runtime_skill.py
- Modify: README.md
- Modify: README.zh-CN.md
- Modify locally only: ignored config.yaml

**Interfaces:**
- Agent runs python <runtime-root>/scripts/resolve_cjk_font.py --config <runtime-root>/rendering-config.json.
- Smoke helper accepts --font and --output-dir and writes cjk-smoke.pdf/png.

- [ ] **Step 1: Write failing skill tests**

~~~python
assert "cjk-rendering.md" in skill
assert "resolve_cjk_font.py" in skill
assert "rendering-config.json" in skill
for needle in (
    "显式指定",
    "字体嵌入",
    "渲染后验证",
    "禁止下载或安装字体",
    "找不到可用中文字体",
):
    assert needle in cjk_reference
~~~

Assert office and visual references route Chinese PDF/image work to cjk-rendering.md.

- [ ] **Step 2: Verify RED**

Run: .venv/bin/pytest tests/test_runtime_skill.py -q

Expected: missing reference and contracts.

- [ ] **Step 3: Write the focused reference**

Keep SKILL.md as an index. The reference must require:

1. Run the bundled resolver before rendering Chinese.
2. Pass the returned path explicitly to PIL ImageFont.truetype, ReportLab TTFont, Matplotlib FontProperties(fname=...), PyMuPDF insert_font(fontfile=...), or browser @font-face.
3. Never trust Arial, Helvetica, DejaVu Sans, or generic sans-serif as the only Chinese font.
4. Embed/subset fonts in PDF where supported and inspect reopened font metadata or a rasterized page.
5. Inspect generated image/PDF; source code and exit code are insufficient.
6. Block honestly when resolution or verification fails. Never download/install a replacement.

Office and visual references link to this file without duplicating the procedure.

- [ ] **Step 4: Verify GREEN**

Run: .venv/bin/pytest tests/test_runtime_skill.py -q

Expected: all tests pass.

- [ ] **Step 5: Add and run opt-in real smoke test**

The helper uses PyMuPDF to insert the configured font, writes 中文字体测试, reopens the PDF, asserts exact text extraction, confirms an embedded Type0 font in get_fonts(full=True), rasterizes the page, checks pixel samples are nonuniform, and saves PNG.

The pytest wrapper skips unless QQ_AGENT_BRIDGE_CJK_SMOKE=1, then executes the helper inside micromamba base. Run:

~~~bash
QQ_AGENT_BRIDGE_CJK_SMOKE=1 .venv/bin/pytest tests/test_cjk_rendering_smoke.py -v
~~~

Expected: one pass with nonempty PDF and PNG.

- [ ] **Step 6: Document and configure**

Document configured precedence, no-fallback behavior, read-only path visibility, Noto/Source Han/WenQuanYi examples, and that /reload affects future jobs.

Add to ignored local config.yaml and do not stage it:

~~~yaml
rendering:
  cjk_font_files:
    - "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
  auto_detect_cjk_font: true
~~~

- [ ] **Step 7: Full verification**

~~~bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src skills/qq-agent-runtime/scripts tests/helpers
git diff --check
~~~

Expected: suite passes apart from pre-existing date-bound skips; compileall and diff check exit zero.

- [ ] **Step 8: Commit**

~~~bash
git add skills/qq-agent-runtime tests/test_runtime_skill.py tests/helpers/cjk_render_smoke.py tests/test_cjk_rendering_smoke.py README.md README.zh-CN.md
git commit -m "feat: teach agents reliable Chinese rendering"
~~~

---

### Task 6: Adversarial Review And Regression

**Files:**
- Review every file changed in Tasks 1-5.
- Modify only files needed for confirmed findings.

- [ ] **Step 1: Review hostile cases**

Inspect malformed YAML, directories/symlinks, explicit fonts without zh coverage, missing fontconfig tools, shell metacharacters in paths, concurrent manifest writes, /reload, external paths, hidden downloads, false PDF success claims, and private config exposure.

- [ ] **Step 2: For every confirmed finding, add a failing regression test first**

Run the narrow test and observe the intended failure before editing production code.

- [ ] **Step 3: Apply the smallest fix and rerun focused tests**

Do not bundle unrelated cleanup.

- [ ] **Step 4: Final verification**

~~~bash
QQ_AGENT_BRIDGE_CJK_SMOKE=1 .venv/bin/pytest tests/test_cjk_rendering_smoke.py -v
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src skills/qq-agent-runtime/scripts tests/helpers
git diff --check
git status --short
~~~

Expected: smoke and full suite pass, compileall/diff check exit zero, and status shows only intentional work plus pre-existing /mode changes.

- [ ] **Step 5: Commit review fixes if any**

Stage only files associated with verified findings and commit with message:

~~~text
fix: harden CJK font rendering
~~~
