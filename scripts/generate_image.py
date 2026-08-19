#!/usr/bin/env python3
"""Generate an image through the configured OpenCC gpt-image-2 provider."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


MODEL = "gpt-image-2"
EXPECTED_HOST = "opencc.yiminju.xyz"
RETRYABLE_STATUS = {502, 503, 504}


class SafeError(RuntimeError):
    """An error whose message is safe to show after sanitization."""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, help="Complete image instruction")
    parser.add_argument(
        "--reference-image",
        action="append",
        default=[],
        help="Local PNG, JPEG, WebP, or GIF reference; repeat for multiple images",
    )
    parser.add_argument("--output-dir", default="outputs", help="Project-local output directory")
    parser.add_argument("--output-name", default="opencc-image.png", help="Desired file name")
    parser.add_argument("--timeout", type=int, default=300, help="Request timeout in seconds")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate configuration and inputs without sending a request",
    )
    return parser.parse_args()


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def strip_toml_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote:
            return value[:index].rstrip()
    return value.strip()


def unquote_toml_string(value: str) -> str:
    value = strip_toml_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    raise SafeError("Codex config contains a non-string provider setting")


def load_provider_config(config_path: Path) -> Tuple[str, str]:
    if not config_path.is_file():
        raise SafeError(f"Codex config was not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    provider_match = re.search(r"(?m)^\s*model_provider\s*=\s*(.+?)\s*$", text)
    if not provider_match:
        raise SafeError("model_provider is missing from Codex config.toml")
    provider = unquote_toml_string(provider_match.group(1))

    current_section = ""
    base_url = ""
    target_section = f"model_providers.{provider}"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        section_match = re.match(r"^\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            current_section = section_match.group(1).strip().replace('"', "").replace("'", "")
            continue
        if current_section != target_section:
            continue
        base_match = re.match(r"^base_url\s*=\s*(.+?)\s*$", line)
        if base_match:
            base_url = unquote_toml_string(base_match.group(1))
            break

    if not base_url:
        raise SafeError(f"base_url is missing for model provider {provider}")
    return provider, base_url


def load_api_key(auth_path: Path) -> str:
    if not auth_path.is_file():
        raise SafeError(f"Codex auth file was not found: {auth_path}")
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafeError("Codex auth.json is not valid JSON") from exc
    api_key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        raise SafeError("OPENAI_API_KEY is missing from Codex auth.json")
    return api_key.strip()


def build_endpoint(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url.strip())
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != EXPECTED_HOST:
        raise SafeError(f"Active provider must use https://{EXPECTED_HOST}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SafeError("OpenCC base_url must not contain credentials, query parameters, or fragments")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = f"{path}/chat/completions"
    else:
        path = f"{path}/v1/chat/completions"
    return urllib.parse.urlunparse(("https", EXPECTED_HOST, path, "", "", ""))


def detect_image_type(data: bytes) -> Tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    raise SafeError("A supplied or returned file is not a supported PNG, JPEG, WebP, or GIF image")


def reference_content(paths: Iterable[str]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise SafeError(f"Reference image was not found: {path}")
        data = path.read_bytes()
        if not data:
            raise SafeError(f"Reference image is empty: {path}")
        mime_type, _ = detect_image_type(data)
        encoded = base64.b64encode(data).decode("ascii")
        result.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            }
        )
    return result


def build_payload(prompt: str, references: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not prompt.strip():
        raise SafeError("The image prompt cannot be empty")
    content: Any = prompt.strip()
    if references:
        content = [{"type": "text", "text": prompt.strip()}, *references]
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }


def sanitize_error(value: str) -> str:
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", value)
    value = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "data:image/[REDACTED]", value)
    value = re.sub(r"([?&](?:token|key|signature|sig|auth)=[^&\s]+)", "?[REDACTED]", value, flags=re.I)
    return value[:1200]


def request_json(endpoint: str, api_key: str, payload: Dict[str, Any], timeout: int) -> Tuple[Dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    delays = (0, 5, 10)
    last_error: Optional[SafeError] = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "opencc-image2-skill/1.0.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise SafeError("OpenCC returned a non-JSON response") from exc
            if not isinstance(parsed, dict):
                raise SafeError("OpenCC returned an unexpected JSON response")
            return parsed, attempt
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = sanitize_error(detail or str(exc.reason))
            last_error = SafeError(
                f"OpenCC request failed with HTTP {exc.code}: {message}",
                status=exc.code,
                retryable=exc.code in RETRYABLE_STATUS,
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = exc.reason if hasattr(exc, "reason") else exc
            last_error = SafeError(
                f"OpenCC connection failed: {sanitize_error(str(reason))}",
                retryable=True,
            )
        if last_error and (not last_error.retryable or attempt == len(delays)):
            raise last_error
    raise last_error or SafeError("OpenCC request failed")


def candidate_from_string(value: str) -> Optional[Tuple[str, str]]:
    data_match = re.search(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", value)
    if data_match:
        return "data", data_match.group(0)
    markdown_match = re.search(r"!\[[^]]*]\((https://[^)\s]+)\)", value)
    if markdown_match:
        return "url", markdown_match.group(1)
    url_match = re.search(r"https://[^\s<>'\"]+", value)
    if url_match:
        return "url", url_match.group(0).rstrip(").,;")
    return None


def find_image_candidate(response: Dict[str, Any]) -> Tuple[str, str]:
    data = response.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if isinstance(data[0].get("url"), str):
            return "url", data[0]["url"]
        if isinstance(data[0].get("b64_json"), str):
            return "base64", data[0]["b64_json"]

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise SafeError("OpenCC response did not contain image output")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    images = message.get("images") if isinstance(message, dict) else None
    if isinstance(images, list):
        for item in images:
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                return candidate_from_string(image_url["url"]) or ("url", image_url["url"])
            if isinstance(image_url, str):
                return candidate_from_string(image_url) or ("url", image_url)
    if isinstance(content, str):
        candidate = candidate_from_string(content)
        if candidate:
            return candidate
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                return candidate_from_string(image_url["url"]) or ("url", image_url["url"])
            if isinstance(image_url, str):
                return candidate_from_string(image_url) or ("url", image_url)
            if isinstance(item.get("url"), str):
                return candidate_from_string(item["url"]) or ("url", item["url"])
            if isinstance(item.get("b64_json"), str):
                return "base64", item["b64_json"]
            if isinstance(item.get("text"), str):
                candidate = candidate_from_string(item["text"])
                if candidate:
                    return candidate
    raise SafeError("OpenCC response did not contain a downloadable image")


def image_bytes(candidate: Tuple[str, str], timeout: int) -> bytes:
    kind, value = candidate
    if kind == "base64":
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise SafeError("OpenCC returned invalid Base64 image data") from exc
    if kind == "data":
        match = re.match(r"^data:image/[^;]+;base64,(.+)$", value, flags=re.S)
        if not match:
            raise SafeError("OpenCC returned an invalid image Data URL")
        try:
            return base64.b64decode(match.group(1), validate=True)
        except ValueError as exc:
            raise SafeError("OpenCC returned invalid Base64 image data") from exc
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() != "https":
        raise SafeError("OpenCC returned a non-HTTPS image URL")
    try:
        request = urllib.request.Request(value, headers={"User-Agent": "opencc-image2-skill/1.0.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise SafeError(f"Generated image download failed: {sanitize_error(str(exc))}") from exc


def collision_free_path(output_dir: Path, output_name: str, extension: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    leaf = Path(output_name).name
    if not leaf or leaf in {".", ".."}:
        leaf = f"opencc-image{extension}"
    requested = Path(leaf)
    stem = requested.stem or "opencc-image"
    candidate = output_dir / f"{stem}{extension}"
    version = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}-v{version}{extension}"
        version += 1
    return candidate


def main() -> int:
    args = parse_args()
    if args.timeout < 30:
        raise SafeError("Timeout must be at least 30 seconds")

    home = codex_home()
    provider, base_url = load_provider_config(home / "config.toml")
    api_key = load_api_key(home / "auth.json")
    endpoint = build_endpoint(base_url)
    references = reference_content(args.reference_image)
    payload = build_payload(args.prompt, references)

    if args.validate_only:
        print(
            json.dumps(
                {
                    "ok": True,
                    "validate_only": True,
                    "provider": provider,
                    "provider_host": EXPECTED_HOST,
                    "endpoint_path": urllib.parse.urlparse(endpoint).path,
                    "model": MODEL,
                    "reference_count": len(references),
                },
                ensure_ascii=False,
            )
        )
        return 0

    response, attempts = request_json(endpoint, api_key, payload, args.timeout)
    data = image_bytes(find_image_candidate(response), args.timeout)
    if not data:
        raise SafeError("OpenCC returned an empty image")
    _, extension = detect_image_type(data)
    output_path = collision_free_path(Path(args.output_dir), args.output_name, extension)
    output_path.write_bytes(data)

    print(
        json.dumps(
            {
                "ok": True,
                "provider": provider,
                "provider_host": EXPECTED_HOST,
                "endpoint_path": urllib.parse.urlparse(endpoint).path,
                "model": MODEL,
                "output_path": str(output_path),
                "size_bytes": len(data),
                "attempts": attempts,
                "reference_count": len(references),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafeError as exc:
        print(json.dumps({"ok": False, "error": sanitize_error(str(exc)), "status": exc.status}), file=sys.stderr)
        raise SystemExit(1)
