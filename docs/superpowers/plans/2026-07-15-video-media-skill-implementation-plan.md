# Video Media Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make video understanding, especially Bilibili and Chinese video sites, evidence-driven and login-aware so the agent never summarizes a video from its title or a similar search result.

**Architecture:** Keep acquisition and media analysis in the agent runtime, but make the skill a strict decision procedure. The skill distinguishes public metadata, authenticated page access, subtitles, audio transcription, frames, and background search. Tests assert the rule text is loaded into task prompts and explicitly covers login walls, rate limits, short-link resolution, and evidence coverage.

**Tech Stack:** Markdown skills, runtime skill bundling, pytest, existing Cursor/Codex/custom CLI task runtime, configured local Whisper runner, ffmpeg/yt-dlp when actually available.

## Global Constraints

- A URL is an entry point, not evidence that the agent has read the media.
- Titles, search snippets, comments, similar-topic articles, and general knowledge cannot support a claim about the video itself.
- A login wall, cookie requirement, CAPTCHA, HTTP 401/403/412/429, or unavailable subtitle/audio endpoint is an explicit acquisition failure.
- The agent must not bypass access controls, invent cookies, claim to have watched a video, or summarize unverified content.
- Bilibili `b23.tv` short links must be resolved before evidence assessment.
- If only metadata is available, report verified metadata and the missing evidence; do not produce a content summary.
- Intermediate QQ progress must be short and descriptive, never raw tool logs or repetitive “step complete” text.

---

### Task 1: Write skill regression tests for evidence and login handling

**Files:**
- Create: `tests/test_visual_media_skill.py`
- Modify: `tests/test_runtime_skill.py`

**Interfaces:**
- Tests read `skills/qq-agent-runtime/references/visual-media.md` and the bundled runtime skill output.
- The test contract requires the exact concepts `b23.tv`, `登录`, `403`, `429`, `字幕`, `转写`, `抽帧`, `不能只凭标题`, and `未验证` to be present.

- [ ] **Step 1: Write failing tests**

Add static tests:

```python
def test_visual_media_skill_requires_real_video_evidence():
    text = Path("skills/qq-agent-runtime/references/visual-media.md").read_text(encoding="utf-8")
    for phrase in ("b23.tv", "字幕", "转写", "抽帧", "不能只凭标题", "未验证"):
        assert phrase in text

def test_visual_media_skill_treats_login_and_rate_limits_as_blockers():
    text = Path("skills/qq-agent-runtime/references/visual-media.md").read_text(encoding="utf-8")
    assert "登录" in text
    assert "403" in text
    assert "429" in text
    assert "不要绕过" in text

def test_task_bundle_includes_login_aware_media_rules():
    bundle = build_runtime_skill("task")
    assert "字幕" in bundle
    assert "未验证" in bundle
```

Use the existing `runtime_skill.py` test fixture to build the bundle; do not invoke a real agent.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_visual_media_skill.py tests/test_runtime_skill.py
```

Expected: FAIL because the current media skill does not state the complete login contract.

- [ ] **Step 3: Keep the test assertions narrow**

Do not assert a particular vendor tool name, browser implementation, or model. The contract is evidence, access-control honesty, and Chinese video-site handling; the agent may use any available tool that actually produces evidence.

### Task 2: Replace the video section with an explicit evidence state machine

**Files:**
- Modify: `skills/qq-agent-runtime/references/visual-media.md`
- Modify: `skills/qq-agent-runtime/SKILL.md`
- Modify: `src/qq_agent_bridge/runtime_skill.py` only if the short index omits the new rules

**Interfaces:**
- The reference remains the authoritative detailed media procedure.
- The top-level skill index points to it for `B站/b23.tv/网页视频` and does not duplicate contradictory rules.

- [ ] **Step 1: Write the procedure before implementation**

Use these evidence states in the skill:

```text
入口 -> canonical URL -> public access check -> evidence collection -> evidence coverage check -> answer
```

The procedure must say:

1. Resolve `b23.tv` and other short links to a canonical page.
2. Try public metadata, page正文, chapters, and subtitles without assuming login.
3. Detect login or anti-bot blockers from visible login prompts and HTTP 401/403/412/429, CAPTCHA, cookie-required, or empty protected endpoints.
4. Do not bypass the blocker or fabricate a session. Tell the user exactly which evidence layer is unavailable.
5. If subtitles are available, use them and report their coverage.
6. If subtitles are absent but an accessible media stream exists, extract audio and use the configured local Whisper runner or another actually available transcriber.
7. If visual claims matter, sample multiple meaningful time ranges and inspect frames/OCR, not just the cover.
8. Use comments and search results only as background, marked “未验证为视频内容”.
9. Summarize only observations supported by evidence. When no content evidence exists, output metadata plus a limitation instead of a summary.

- [ ] **Step 2: Implement the skill text**

Keep QQ-facing guidance short. For a task, permitted progress examples are “已解析短链，正在检查公开字幕” and “字幕不可用，正在尝试实际音频转写”; do not forward command logs, credentials, cookies, or local paths. Add an explicit anti-hallucination example where a misleading title describes a pet video, and state that title semantics cannot override missing evidence.

Keep the existing send rules: generated images use `QQBOT_SEND_IMAGE`, source/PDF/document files use `QQBOT_SEND_FILE`, and a video summary is not complete merely because a page title was found.

- [ ] **Step 3: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_visual_media_skill.py tests/test_runtime_skill.py tests/test_prompting.py -k "video or media or skill"
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add skills/qq-agent-runtime/references/visual-media.md skills/qq-agent-runtime/SKILL.md src/qq_agent_bridge/runtime_skill.py tests/test_visual_media_skill.py tests/test_runtime_skill.py
git commit -m "feat: make video skill login and evidence aware"
```

### Task 3: Add video access failure and evidence examples to the runtime prompt contract

**Files:**
- Modify: `src/qq_agent_bridge/prompting.py`
- Test: `tests/test_prompting.py`

**Interfaces:**
- Task prompts continue to use `build_agent_prompt`; no new command or preflight model call is added.
- The prompt tells the task agent to recommend a higher-level task only when a short ask cannot perform the requested media operation, without adding a bridge-side interception.

- [ ] **Step 1: Write failing prompt tests**

Add a test that builds a task prompt containing a Bilibili URL and asserts it contains login-wall behavior and the requirement to distinguish metadata from media evidence. Add a test that a task prompt says a failed page access cannot be replaced by a similar search result.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_prompting.py -k "video or evidence or login"
```

Expected: FAIL until the prompt contract includes the new rules.

- [ ] **Step 3: Implement the minimum prompt additions**

Add concise rules to the existing media section, not a second independent video prompt. The additions must explicitly require:

```text
遇到登录墙、验证码、401/403/412/429、cookie required 或字幕/音频接口不可访问时，说明阻塞；不要绕过、不要伪造登录态、不要把标题或搜索结果当视频内容。
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/pytest -q tests/test_prompting.py -k "video or evidence or login"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/qq_agent_bridge/prompting.py tests/test_prompting.py
git commit -m "feat: add login-aware video prompt rules"
```

### Task 4: Validate the skill without network or account state

**Files:**
- Create: `tests/fixtures/media/README.md`
- Modify: `tests/test_visual_media_skill.py`

**Interfaces:**
- Tests use deterministic fixture descriptions, not private cookies or live video pages.

- [ ] **Step 1: Add fixture scenarios**

Document these four cases in `tests/fixtures/media/README.md`: public page with subtitles, public page without subtitles but downloadable audio, login-required page returning 403, and short-link page resolving only to metadata. The fixture file contains no URLs requiring authentication.

- [ ] **Step 2: Add deterministic decision tests**

Test a pure helper in the test module that maps evidence flags to allowed answer mode:

```python
assert answer_mode(metadata=True, subtitles=False, transcript=False, frames=False) == "metadata-only"
assert answer_mode(metadata=True, subtitles=True, transcript=False, frames=False) == "content-summary"
assert answer_mode(metadata=False, subtitles=False, transcript=False, frames=False) == "blocked"
```

This helper tests the documented decision table, not a second production parser.

- [ ] **Step 3: Run the full skill checks**

Run:

```bash
.venv/bin/pytest -q tests/test_visual_media_skill.py tests/test_runtime_skill.py tests/test_prompting.py -k "video or media or skill or evidence or login"
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/media/README.md tests/test_visual_media_skill.py
git commit -m "test: cover video evidence access states"
```

### Task 5: Perform the final media and security review

**Files:**
- Review: `skills/qq-agent-runtime/references/visual-media.md`
- Review: `skills/qq-agent-runtime/SKILL.md`
- Review: `src/qq_agent_bridge/prompting.py`
- Review: `tests/test_visual_media_skill.py`

**Interfaces:**
- No code changes are allowed during the review unless a failing test identifies a concrete contradiction.

- [ ] **Step 1: Search for unsafe claims**

Run:

```bash
rg -n "看过|已观看|肯定|一定|根据标题|绕过|伪造|cookie|密码|token" skills/qq-agent-runtime src/qq_agent_bridge/prompting.py
```

Review every match and remove any instruction that could claim inaccessible evidence or expose credentials.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit only if the review found and fixed a concrete issue**

```bash
git add skills/qq-agent-runtime src/qq_agent_bridge/prompting.py tests/test_visual_media_skill.py tests/test_runtime_skill.py tests/test_prompting.py tests/fixtures/media/README.md
git commit -m "review: harden video evidence handling"
```
