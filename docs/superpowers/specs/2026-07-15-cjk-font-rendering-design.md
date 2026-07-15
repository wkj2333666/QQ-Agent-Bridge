# Configurable CJK Font Rendering Design

## Goal

Make Chinese text render reliably in generated images and PDFs without bundling a font or allowing the QQ agent to modify the host system. Operators choose trusted font files in configuration; automatic fontconfig discovery remains an explicit fallback for portable deployments.

## Scope

This change covers artifacts created by `/task` and `/code` through the injected `qq-agent-runtime` skill. It does not install fonts, download fonts, rewrite arbitrary user documents, or guarantee that every third-party renderer can embed every font format.

## Configuration

Add a top-level rendering section:

```yaml
rendering:
  cjk_font_files: []
  auto_detect_cjk_font: true
```

`cjk_font_files` is an ordered list of operator-controlled font paths. When the list is non-empty, the bridge validates and uses only those entries; it does not silently replace an invalid configured font with an unrelated system font. When the list is empty, `auto_detect_cjk_font: true` permits deterministic discovery through fontconfig. With an empty list and automatic discovery disabled, CJK rendering is unavailable and the agent must report that limitation.

The private deployment configuration will set:

```yaml
rendering:
  cjk_font_files:
    - "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
  auto_detect_cjk_font: true
```

The public example remains host-neutral and documents representative Noto, Source Han, and WenQuanYi paths without installing anything.

## Components

### Bridge configuration

`RenderingConfig` owns the ordered font paths and discovery switch. Loading preserves list order, rejects non-string values, and does not mutate the filesystem. Reloading configuration updates the effective font selection for future jobs.

### Font resolver

A dependency-free helper in the runtime skill bundle resolves one usable CJK font and emits machine-readable output containing its path and family. Resolution follows these rules:

1. Validate explicit configured paths in order.
2. Confirm that a candidate is a readable regular file and that fontconfig reports Chinese coverage.
3. If and only if no paths were configured and automatic discovery is enabled, query fontconfig and rank Noto/Source Han first, WenQuanYi next, and Unifont last.
4. Fail with a precise diagnostic when no candidate is usable.

The resolver never downloads, installs, copies, or edits a font.

### Sandbox exposure

The bubblewrap command exposes `/etc/fonts` and the existing system fontconfig cache read-only when present. Font files under `/usr/share/fonts` are already covered by the read-only `/usr` bind. Explicit paths outside existing read-only system binds are not mounted automatically; validation reports that they are inaccessible rather than widening the sandbox silently.

### Runtime skill

Add a focused `cjk-rendering.md` reference and route image/PDF work to it from the office-document and visual-media references. The skill requires the agent to:

- resolve a configured CJK font before rendering Chinese text;
- pass the resolved file or family explicitly to PIL, ReportLab, Matplotlib, browser CSS, or the selected renderer;
- embed or subset the font in PDFs when the renderer and font permit it;
- verify the finished artifact, not merely the source code or command exit status;
- report a blocked result rather than claim success when glyph coverage or rendering cannot be verified.

The bridge copies both references and helper scripts into the workspace-local runtime skill bundle. It also writes a generated `rendering-config.json` manifest containing only the ordered font paths and automatic-discovery switch. The prompt names the relative helper and manifest locations; it does not expose the full bridge configuration or unrelated secrets.

## Data Flow

1. The bridge loads and validates `rendering` configuration.
2. A task/code job prepares the workspace-local runtime skill bundle.
3. The bridge writes the ordered configured font candidates and discovery policy to the generated runtime manifest.
4. The agent invokes the resolver before creating a Chinese image or PDF.
5. The renderer receives an explicit selected font.
6. The agent verifies the artifact and sends it only after the existing outbox checks pass.

## Error Handling

- A configured path that is missing, unreadable, not a regular file, or lacks Chinese coverage produces a clear configuration/rendering failure.
- A missing `fc-list`/`fc-query` disables automatic discovery and coverage validation; it does not trigger a download or an unverified fallback.
- A renderer that cannot use the selected font must switch to another available renderer or report the block.
- PDF embedding should be verified where tooling permits. If verification tooling is absent, the agent must at least perform a raster or text-presence smoke check and state any remaining uncertainty.

## Testing

Implementation follows test-driven development:

- configuration loading and invalid-value tests;
- resolver tests for explicit precedence, invalid explicit paths, deterministic discovery ranking, and no-font failure;
- runtime bundle tests proving helper scripts and the new reference are copied;
- bubblewrap command tests proving fontconfig paths are read-only and conditional;
- prompt/skill tests proving explicit selection, PDF embedding guidance, verification, and no-install boundaries;
- environment smoke tests that render Chinese into an image and a PDF using the configured WenQuanYi font, then inspect rendered pixels/text or font metadata with available local tools.

The full existing test suite must remain green. Environment-dependent smoke tests skip with a specific reason only when their required renderer is genuinely unavailable.

## Security And Portability

The feature adds no network access and no package-manager calls. Operators retain control over font provenance. The bridge grants no new write access, and it refuses to auto-bind arbitrary configured host paths. Open-source users can choose system packages appropriate for their distribution while receiving deterministic diagnostics when setup is incomplete.
