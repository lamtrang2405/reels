"""Gemini image generation: summary → prompt → image, and image-to-image regenerate."""

from __future__ import annotations

import os
from io import BytesIO
from threading import Lock

from PIL import Image

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

SUPPORTED_ASPECTS = ("9:16", "4:5", "1:1")
# Comma-separated chains: the next model is tried when one is overloaded or unavailable.
DEFAULT_IMAGE_MODELS = "gemini-3.1-flash-image,gemini-2.5-flash-image"
DEFAULT_TEXT_MODELS = "gemini-flash-latest,gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3.5-flash-lite"
REQUEST_TIMEOUT_MS = 120_000
IMAGE_TIMEOUT_MS = 120_000
# Errors that mean "this model can't serve now" → try the next model in the chain.
_FALLBACK_MARKERS = (
    "503", "unavailable", "overloaded", "high demand",
    "504", "deadline", "timed out", "timeout",
    "404", "not_found", "no longer available", "not found",
    "429", "resource_exhausted",
)

_CLEAN_IMAGE_RULES = (
    "The image must contain no text, no letters, no captions, no logos and no watermark. "
    "Photorealistic or high-quality illustration suitable for a social media post, "
    "clear focal subject, good lighting, rich detail."
)

_client = None
_client_lock = Lock()


class AIImageError(Exception):
    """User-facing error (message in Vietnamese)."""


def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def ai_enabled() -> bool:
    return bool(_api_key())


def _model_chain(env_name: str, default: str) -> list[str]:
    raw = os.environ.get(env_name, "").strip() or default
    return [m.strip() for m in raw.split(",") if m.strip()]


def image_models() -> list[str]:
    return _model_chain("GEMINI_IMAGE_MODEL", DEFAULT_IMAGE_MODELS)


def text_models() -> list[str]:
    return _model_chain("GEMINI_TEXT_MODEL", DEFAULT_TEXT_MODELS)


def image_model() -> str:
    return image_models()[0]


def text_model() -> str:
    return text_models()[0]


def generate_with_fallback(models: list[str], timeout_ms: int = 45_000, **kwargs):
    """client.models.generate_content over a model chain; returns the first successful response.

    Non-final models get a single attempt: the SDK's own 503 backoff takes ~80s, which would
    stall the chain instead of moving on to the next model.
    """
    from google.genai import types

    client = _get_client()
    base_config = kwargs.pop("config", None) or types.GenerateContentConfig()
    last_err: Exception | None = None
    for i, model in enumerate(models):
        is_last = i == len(models) - 1
        http = types.HttpOptions(timeout=timeout_ms)
        if not is_last:
            http.retry_options = types.HttpRetryOptions(attempts=1)
        config = base_config.model_copy(update={"http_options": http})
        try:
            return client.models.generate_content(model=model, config=config, **kwargs)
        except Exception as err:  # noqa: BLE001
            low = str(err).lower()
            if any(m in low for m in _FALLBACK_MARKERS):
                last_err = err
                continue
            raise
    if last_err is not None:
        raise last_err
    raise AIImageError("Chưa cấu hình model Gemini.")


def normalize_aspect(value: str | None) -> str:
    v = (value or "").strip().replace("x", ":").replace("/", ":")
    return v if v in SUPPORTED_ASPECTS else SUPPORTED_ASPECTS[0]


def _get_client():
    global _client
    if not ai_enabled():
        raise AIImageError("Chưa cấu hình GEMINI_API_KEY trên server.")
    with _client_lock:
        if _client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as err:
                raise AIImageError("Thiếu thư viện google-genai (pip install google-genai).") from err
            _client = genai.Client(
                api_key=_api_key(),
                http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
            )
        return _client


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AIImageError:
        raise
    except Exception as err:  # noqa: BLE001
        msg = str(err)
        low = msg.lower()
        if "api key" in low or "permission" in low or "401" in low or "403" in low:
            raise AIImageError(f"Gemini từ chối API key: {msg[:300]}") from err
        if "429" in low or "quota" in low or "resource_exhausted" in low:
            raise AIImageError("Gemini báo vượt hạn mức (quota/rate limit). Thử lại sau.") from err
        if "timeout" in low or "timed out" in low or "deadline" in low:
            raise AIImageError("Gemini phản hồi quá lâu (timeout). Thử lại.") from err
        raise AIImageError(f"Lỗi gọi Gemini: {msg[:300]}") from err


def _extract_image(response) -> Image.Image:
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise AIImageError(f"Gemini chặn yêu cầu (safety): {feedback.block_reason}")

    candidates = getattr(response, "candidates", None) or []
    texts: list[str] = []
    for cand in candidates:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                im = Image.open(BytesIO(inline.data))
                im.load()
                return im.convert("RGB")
            if getattr(part, "text", None):
                texts.append(part.text)

    reason = ""
    if candidates:
        fr = getattr(candidates[0], "finish_reason", None)
        if fr:
            reason = f" (finish_reason={fr})"
    hint = f" Gemini trả về: {' '.join(texts)[:300]}" if texts else ""
    raise AIImageError(f"Gemini không trả về ảnh{reason}.{hint}")


def summary_to_prompt(summary: str, style: str = "") -> str:
    """Turn a user summary (any language) into an English visual prompt for a clean image."""
    summary = (summary or "").strip()
    if not summary:
        raise AIImageError("Phần tóm tắt đang trống.")
    style_line = f"Preferred visual style: {style.strip()}.\n" if style and style.strip() else ""
    instruction = (
        "You are an art director for social media posts (Facebook, TikTok). "
        "Read the content summary below and write ONE image-generation prompt in English "
        "(60-120 words) describing a single striking scene that represents the main idea. "
        "Describe subject, setting, composition, lighting, mood and color palette. "
        "Do not include any text, typography or logos in the scene. "
        "Return only the prompt, no preamble.\n"
        f"{style_line}"
        f"Summary:\n{summary}"
    )
    response = _call(generate_with_fallback, text_models(), contents=instruction)
    prompt = (getattr(response, "text", None) or "").strip()
    if not prompt:
        raise AIImageError("Gemini không tạo được prompt từ tóm tắt.")
    return prompt


def generate_from_prompt(prompt: str, aspect: str) -> Image.Image:
    from google.genai import types

    contents = f"{prompt.strip()}\n\n{_CLEAN_IMAGE_RULES}"
    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=normalize_aspect(aspect)),
    )
    response = _call(generate_with_fallback, image_models(), timeout_ms=IMAGE_TIMEOUT_MS, contents=contents, config=config)
    return _extract_image(response)


def recompose_to_aspect(img: Image.Image, aspect: str) -> Image.Image:
    """Same scene re-framed for another aspect ratio (extends background instead of cropping)."""
    from google.genai import types

    text = (
        "Re-frame this exact image for the requested aspect ratio. Keep the same subject, "
        "style, colors and lighting; extend or adjust the background naturally so nothing "
        f"important is cut off.\n\n{_CLEAN_IMAGE_RULES}"
    )
    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=normalize_aspect(aspect)),
    )
    response = _call(generate_with_fallback, image_models(), timeout_ms=IMAGE_TIMEOUT_MS, contents=[img, text], config=config)
    return _extract_image(response)


def regenerate_from_image(image_bytes: bytes, instruction: str, aspect: str) -> Image.Image:
    """Re-create the source image as a fresh, clean social-media visual (keeps the subject)."""
    from google.genai import types

    if not image_bytes:
        raise AIImageError("Ảnh gốc rỗng.")
    src = Image.open(BytesIO(image_bytes))
    src.load()
    src = src.convert("RGB")
    user_hint = instruction.strip() if instruction and instruction.strip() else (
        "Make it more eye-catching and professional, improve lighting and colors."
    )
    text = (
        "Regenerate this image as a new high-quality visual for a social media post. "
        "Keep the main subject and overall idea recognizable, remove any existing text, "
        "captions, stickers or watermarks, and recompose it to fit the requested frame. "
        f"Instruction: {user_hint}\n\n{_CLEAN_IMAGE_RULES}"
    )
    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=normalize_aspect(aspect)),
    )
    response = _call(generate_with_fallback, image_models(), timeout_ms=IMAGE_TIMEOUT_MS, contents=[src, text], config=config)
    return _extract_image(response)
