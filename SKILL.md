---
name: opencc-image2
description: Generate, revise, or restyle images through the user's configured OpenCC provider with the fixed gpt-image-2 model. Use for text-to-image, reference-image generation, multiple-reference composition, posters, covers, logos, illustrations, and image revisions when output must go through OpenCC rather than a built-in image tool.
---

# OpenCC Image2

Route every image task through the bundled deterministic client. Do not call a built-in image-generation tool, the Responses `image_generation` tool, another provider, or another model.

## Execute

1. Preserve every source image.
2. Save results under the current project's `outputs` directory unless the user requests another project-local directory.
3. On Windows, run `scripts/generate_image.ps1` with Windows PowerShell 5.1 or PowerShell 7.
4. On macOS/Linux, run `scripts/generate_image.py` with Python 3.9 or later.
5. Pass every user-supplied local reference image with the repeatable reference-image argument. Never put a local path only in the text prompt.
6. After generation, open the saved image and inspect subject, composition, faces, hands, requested text, unintended text, logos, watermarks, completeness, and visible defects.
7. If inspection fails, improve the prompt and generate a new version. The scripts never overwrite an existing file.
8. Report the model, provider, saved path, reference count, attempt count, and inspection result. Never report credentials or signed result URLs.

### Windows

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill-dir>\scripts\generate_image.ps1" `
  -Prompt "A square Open CC logo" `
  -OutputDir ".\outputs" `
  -OutputName "opencc-logo.png"
```

Add one or more references:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<skill-dir>\scripts\generate_image.ps1" `
  -Prompt "Keep the person from reference one and use the lighting from reference two" `
  -ReferenceImage ".\person.png", ".\lighting.jpg" `
  -OutputDir ".\outputs" `
  -OutputName "portrait.png"
```

### macOS/Linux

```bash
python3 "<skill-dir>/scripts/generate_image.py" \
  --prompt "A square Open CC logo" \
  --output-dir "./outputs" \
  --output-name "opencc-logo.png"
```

Add one or more references:

```bash
python3 "<skill-dir>/scripts/generate_image.py" \
  --prompt "Keep the person from reference one and use the lighting from reference two" \
  --reference-image "./person.png" \
  --reference-image "./lighting.jpg" \
  --output-dir "./outputs" \
  --output-name "portrait.png"
```

## Routing invariants

- Read `OPENAI_API_KEY` from `auth.json` in the active Codex home.
- Read `model_provider` and its `base_url` from `config.toml`.
- Require the configured host to be exactly `opencc.yiminju.xyz` over HTTPS.
- Send `gpt-image-2` to `/v1/chat/completions`.
- Attach local references as correctly typed Base64 data URLs.
- Retry only network failures and HTTP 502, 503, or 504, at most three total attempts.
- Do not downgrade, switch endpoints, switch models, or switch providers after failure.
- Keep credentials, full Base64 payloads, and signed image URLs out of output.

If the scripts reject configuration, authentication, input, or the upstream response, stop and report the sanitized error. Do not reconstruct the request manually to bypass a guardrail.

## Version

- Current version: `1.0.0`
- Release date: `2026-08-16`
- `1.0.0` (`2026-08-16`): Initial Windows and cross-platform release with text-to-image, multiple reference images, safe configuration loading, retry handling, response parsing, and collision-free output naming.
