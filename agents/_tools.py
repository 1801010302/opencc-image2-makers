"""
OpenCC Image2 工具集 — EdgeOne Makers
调用 opencc.yiminju.xyz 的 gpt-image-2 生成图片
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, List

from agents import function_tool

OPENCC_API_KEY = os.getenv("OPENCC_API_KEY", "").strip()
OPENCC_BASE_URL = os.getenv("OPENCC_BASE_URL", "https://opencc.yiminju.xyz").strip().rstrip("/")
OPENCC_MODEL = os.getenv("OPENCC_MODEL", "gpt-image-2").strip()
TIMEOUT_SEC = int(os.getenv("IMAGE_TIMEOUT_SEC", "300"))


def sanitize_error(value: str) -> str:
    """Strip credentials and base64 from error messages before they reach the model."""
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", value)
    value = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "data:image/[REDACTED]", value)
    value = re.sub(r"([?&](?:token|key|signature|sig|auth)=[^&\s]+)", "?[REDACTED]", value, flags=re.I)
    return value[:1200]


def detect_image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    raise ValueError("Unsupported image format")


def find_image_in_response(response: dict) -> tuple[str, str]:
    """Find image URL or base64 in OpenCC response. Returns (kind, value)."""
    choices = response.get("choices", [])
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message", {})
        content = msg.get("content", "")

        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                img_url = item.get("image_url", {})
                if isinstance(img_url, dict):
                    url = img_url.get("url", "")
                elif isinstance(img_url, str):
                    url = img_url
                else:
                    url = ""
                if url:
                    if url.startswith("data:"):
                        return "data", url
                    if url.startswith("https://"):
                        return "url", url
                b64 = item.get("b64_json", "")
                if b64:
                    return "base64", b64

        if isinstance(content, str):
            # Try base64 data URI
            m = re.search(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", content)
            if m:
                return "data", m.group(0)
            # Try markdown image
            m = re.search(r"!\[.*?\]\((https://[^\)]+)\)", content)
            if m:
                return "url", m.group(1)
            # Try bare URL
            m = re.search(r"https://[^\s<>'\"]+", content)
            if m:
                return "url", m.group(0).rstrip(").,;")
    raise ValueError("No image found in OpenCC response")


@function_tool
def generate_image(
    prompt: Annotated[str, "The detailed image generation prompt in Chinese or English"],
    reference_images: Annotated[
        List[str],
        "Optional list of reference image file paths (local paths) to use as references"
    ] = [],
) -> str:
    """
    Generate an image using OpenCC's gpt-image-2 model via the OpenAI-compatible API.
    Supports text-to-image and reference-image generation.
    The prompt should be detailed and descriptive.
    """
    if not prompt.strip():
        raise ValueError("Prompt cannot be empty")

    if not OPENCC_API_KEY:
        return json.dumps({
            "ok": False,
            "error": "OPENCC_API_KEY is not configured. Please set the OPENCC_API_KEY environment variable."
        }, ensure_ascii=False)

    # Build messages
    if reference_images:
        content = [{"type": "text", "text": prompt.strip()}]
        for path in reference_images:
            path = path.strip()
            if not path:
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                raise ValueError(f"Reference image not found: {path}")
            if not data:
                raise ValueError(f"Reference image is empty: {path}")
            mime, _ = detect_image_type(data)
            b64 = base64.b64encode(data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })
    else:
        content = prompt.strip()

    payload = {
        "model": OPENCC_MODEL,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }

    # Build endpoint
    base = OPENCC_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        endpoint = f"{base}/chat/completions"
    else:
        endpoint = f"{base}/v1/chat/completions"

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    delays = [0, 5, 10]
    last_err = None

    for attempt, delay in enumerate(delays, 1):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(
                endpoint, data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {OPENCC_API_KEY}",
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": "opencc-image2-makers/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                response = json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_err = ValueError(f"OpenCC HTTP {exc.code}: {sanitize_error(detail[:500])}")
            if exc.code not in (502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = ValueError(f"Connection failed: {sanitize_error(str(exc))}")
    else:
        raise last_err

    try:
        kind, value = find_image_in_response(response)
    except ValueError:
        raise ValueError(f"Could not parse image from response: {sanitize_error(str(response)[:500])}")

    if kind == "base64":
        img_data = base64.b64decode(value, validate=True)
    elif kind == "data":
        m = re.match(r"^data:image/[^;]+;base64,(.+)$", value)
        if not m:
            raise ValueError("Invalid base64 data URI")
        img_data = base64.b64decode(m.group(1), validate=True)
    else:  # url
        try:
            img_req = urllib.request.Request(value, headers={"User-Agent": "opencc-image2-makers/1.0"})
            with urllib.request.urlopen(img_req, timeout=TIMEOUT_SEC) as resp:
                img_data = resp.read()
        except Exception as exc:
            raise ValueError(f"Failed to download generated image: {sanitize_error(str(exc))}")

    mime, ext = detect_image_type(img_data)
    import uuid
    filename = f"opencc-{uuid.uuid4().hex[:8]}{ext}"
    out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "wb") as f:
        f.write(img_data)

    _, ext_detected = detect_image_type(img_data)
    return json.dumps({
        "ok": True,
        "model": OPENCC_MODEL,
        "provider_host": OPENCC_BASE_URL,
        "output_path": out_path,
        "size_bytes": len(img_data),
        "reference_count": len(reference_images),
        "message": f"Image saved to {out_path}"
    }, ensure_ascii=False)
