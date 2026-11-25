"""AWS Bedrock AI Service Adapter.

Provides transparent integration with Amazon Bedrock foundation models (Anthropic Claude, etc.)
using AWS_BEARER_TOKEN_BEDROCK, AWS_PROFILE_ID, and AWS_AI_REGION.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_CHEAP_MODEL = "us.anthropic.claude-haiku-5-5"
DEFAULT_HEAVY_MODEL = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
_FALLBACK_CLIENT_ERRORS = ("AccessDeniedException", "ValidationException", "ResourceNotFoundException", "ThrottlingException")
_PERSISTENT_UNAVAILABLE_ERRORS = ("AccessDeniedException", "ValidationException", "ResourceNotFoundException")
_UNAVAILABLE_MODEL_CACHE: set[str] = set()


def is_expensive_model(name: Optional[str]) -> bool:
    low = (name or "").strip().lower()
    if not low or "haiku" in low or "lite" in low:
        return False
    return any(tok in low for tok in ("sonnet", "opus", "-pro", "pro-"))


def get_bedrock_cheap_model() -> str:
    explicit = (os.getenv("BEDROCK_CHEAP_PROFILE_ID") or "").strip()
    if explicit:
        return explicit
    profile = get_bedrock_profile_id()
    if profile and not is_expensive_model(profile):
        return profile
    return DEFAULT_CHEAP_MODEL


def get_bedrock_heavy_model() -> str:
    explicit = (os.getenv("BEDROCK_HEAVY_PROFILE_ID") or "").strip()
    if explicit:
        return explicit
    tier_env = [m.strip() for m in os.getenv("AI_ROUTE_TIER_HEAVY", "").split(",") if m.strip()]
    if tier_env:
        return tier_env[0]
    profile = get_bedrock_profile_id()
    if profile and is_expensive_model(profile):
        return profile
    return DEFAULT_HEAVY_MODEL


def _resolve_bedrock_model(model: Optional[str], content_blocks: List[Dict[str, Any]], system_text: str) -> str:
    if model and "gemini" not in model.lower():
        return model
    text_chars = len(system_text) + sum(len(b.get("text", "")) for b in content_blocks if isinstance(b, dict))
    media_count = sum(1 for b in content_blocks if isinstance(b, dict) and ("image" in b or "document" in b))
    wants_heavy = "pro" in (model or "").lower() or text_chars > 60_000 or media_count >= 5
    return get_bedrock_heavy_model() if wants_heavy else get_bedrock_cheap_model()


def is_bedrock_enabled() -> bool:
    """Return True if AWS Bedrock credentials and profile ID are configured."""
    key = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
    profile = os.getenv("AWS_PROFILE_ID")
    return bool(key and profile)


def get_bedrock_profile_id() -> str:
    return (os.getenv("AWS_PROFILE_ID") or "").strip()


def get_bedrock_region() -> str:
    return (os.getenv("AWS_AI_REGION") or "").strip()


def get_bedrock_key() -> Optional[str]:
    return os.getenv("AWS_BEARER_TOKEN_BEDROCK")


def has_ai_key() -> bool:
    """Return True if AWS Bedrock is configured."""
    return is_bedrock_enabled()


def clean_json_fences(text: str) -> str:
    """Strip markdown code fences from JSON output so json.loads succeeds."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


class BedrockPart:
    def __init__(self, text: str = ""):
        self.text = text
        self.thought = False


class BedrockContent:
    def __init__(self, text: str = ""):
        self.parts = [BedrockPart(text)] if text else []


class BedrockCandidate:
    def __init__(self, text: str = ""):
        self.content = BedrockContent(text)


class BedrockUsageMetadata:
    def __init__(self, prompt_tokens: int = 0, candidates_tokens: int = 0):
        self.prompt_token_count = prompt_tokens
        self.candidates_token_count = candidates_tokens


class BedrockResponse:
    """Emulates google.genai.types.GenerateContentResponse for downstream services."""

    def __init__(self, text: str, usage: Optional[Dict[str, Any]] = None, parsed: Any = None,
                 stop_reason: Optional[str] = None):
        self._text = text
        self.candidates = [BedrockCandidate(text)]
        in_tok = (usage or {}).get("inputTokens", 0)
        out_tok = (usage or {}).get("outputTokens", 0)
        self.usage_metadata = BedrockUsageMetadata(in_tok, out_tok)
        self.parsed = parsed
        self.stop_reason = stop_reason

    @property
    def text(self) -> str:
        return self._text


class BedrockStreamChunk:
    """Single streamed chunk matching the structure expected by assistant_chat_service."""

    def __init__(self, text: str, usage: Optional[Dict[str, Any]] = None):
        self.candidates = [BedrockCandidate(text)]
        in_tok = (usage or {}).get("inputTokens", 0)
        out_tok = (usage or {}).get("outputTokens", 0)
        self.usage_metadata = BedrockUsageMetadata(in_tok, out_tok)


def _extract_text_and_media(contents: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Convert Gemini contents (strings, Parts, Contents, dicts) into Bedrock content blocks."""
    blocks: List[Dict[str, Any]] = []

    def process_item(item: Any):
        if item is None:
            return
        if isinstance(item, str):
            if item.strip():
                blocks.append({"text": item})
            return
        if isinstance(item, (list, tuple)):
            for sub in item:
                process_item(sub)
            return

        if isinstance(item, (bytes, bytearray)):
            b_data = bytes(item)
            if b_data.startswith(b"\x89PNG"):
                blocks.append({"image": {"format": "png", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"%PDF"):
                blocks.append({"document": {"format": "pdf", "name": "document", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"RIFF") and b"WEBP" in b_data[:16]:
                blocks.append({"image": {"format": "webp", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"GIF"):
                blocks.append({"image": {"format": "gif", "source": {"bytes": b_data}}})
                return
            else:
                blocks.append({"image": {"format": "jpeg", "source": {"bytes": b_data}}})
                return

        if "PIL" in getattr(type(item), "__module__", "") and hasattr(item, "save"):
            import io
            buf = io.BytesIO()
            fmt = getattr(item, "format", None) or "JPEG"
            save_fmt = "PNG" if str(fmt).upper() == "PNG" else "JPEG"
            img_to_save = item
            if getattr(item, "mode", "") in ("RGBA", "P") and save_fmt == "JPEG":
                img_to_save = item.convert("RGB")
            img_to_save.save(buf, format=save_fmt)
            blocks.append({"image": {"format": save_fmt.lower(), "source": {"bytes": buf.getvalue()}}})
            return

        # Handle types.Part / types.Content
        inline = getattr(item, "inline_data", None)
        if inline is not None:
            data = getattr(inline, "data", b"")
            b_data = bytes(data) if not isinstance(data, (bytes, bytearray)) else data
            mime = getattr(inline, "mime_type", "") or "image/jpeg"
            if b_data.startswith(b"\x89PNG"):
                blocks.append({"image": {"format": "png", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"\xff\xd8"):
                blocks.append({"image": {"format": "jpeg", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"%PDF"):
                blocks.append({"document": {"format": "pdf", "name": "document", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"RIFF") and b"WEBP" in b_data[:16]:
                blocks.append({"image": {"format": "webp", "source": {"bytes": b_data}}})
                return
            elif b_data.startswith(b"GIF"):
                blocks.append({"image": {"format": "gif", "source": {"bytes": b_data}}})
                return
            elif mime.startswith("image/"):
                fmt = mime.split("/")[-1].lower()
                if fmt == "jpg":
                    fmt = "jpeg"
                if fmt in ("png", "jpeg", "webp", "gif"):
                    blocks.append({"image": {"format": fmt, "source": {"bytes": b_data}}})
                    return
            elif mime == "application/pdf":
                blocks.append({"document": {"format": "pdf", "name": "document", "source": {"bytes": b_data}}})
                return

        parts = getattr(item, "parts", None)
        if parts:
            for p in parts:
                process_item(p)
            return

        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            blocks.append({"text": text})

    process_item(contents)
    if not blocks:
        blocks = [{"text": " "}]
    return blocks, None


def _build_converse_params(
    model: Optional[str],
    contents: Any,
    config: Optional[Any] = None,
) -> Dict[str, Any]:
    """Translate model, contents, and config into boto3 converse parameters."""
    system_instruction = getattr(config, "system_instruction", None)
    system_text = ""
    if system_instruction:
        if isinstance(system_instruction, str):
            system_text = system_instruction
        elif hasattr(system_instruction, "text"):
            system_text = getattr(system_instruction, "text", "")
        elif hasattr(system_instruction, "parts"):
            system_text = "\n".join(getattr(p, "text", "") for p in system_instruction.parts if getattr(p, "text", None))

    is_json = getattr(config, "response_mime_type", None) == "application/json"
    response_schema = getattr(config, "response_schema", None)
    if is_json or response_schema:
        if isinstance(response_schema, (dict, list)):
            try:
                schema_str = json.dumps(response_schema, separators=(",", ":"))
                json_instruction = f"Return ONLY valid JSON matching this schema:{schema_str}. No markdown fences."
            except Exception:
                json_instruction = "Return ONLY valid JSON. No markdown fences."
        elif isinstance(response_schema, str) and response_schema.strip():
            json_instruction = f"Return ONLY valid JSON matching this schema:{response_schema}. No markdown fences."
        else:
            json_instruction = "Return ONLY valid JSON. No markdown fences."
        system_text = f"{system_text}\n{json_instruction}".strip() if system_text else json_instruction

    system = [{"text": system_text}] if system_text else None

    content_blocks, _ = _extract_text_and_media(contents)
    model_id = _resolve_bedrock_model(model, content_blocks, system_text)
    messages = [{"role": "user", "content": content_blocks}]

    max_tokens = getattr(config, "max_output_tokens", None) or 2048
    inference_config: Dict[str, Any] = {"maxTokens": max_tokens}

    params: Dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": inference_config,
    }
    if system:
        params["system"] = system

    return params


class BedrockModels:
    """Mimics client.models on Google GenAI SDK."""

    def __init__(self, bedrock_client: "BedrockClientAdapter"):
        self._adapter = bedrock_client

    def generate_content(
        self,
        *,
        model: Optional[str] = None,
        contents: Any = None,
        config: Optional[Any] = None,
    ) -> BedrockResponse:
        boto = self._adapter.get_boto_client()
        params = _build_converse_params(model, contents, config)
        profile_id = get_bedrock_profile_id()
        if profile_id and params["modelId"] != profile_id and params["modelId"] in _UNAVAILABLE_MODEL_CACHE:
            params["modelId"] = profile_id
        try:
            resp = boto.converse(**params)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if profile_id and params["modelId"] != profile_id and code in _FALLBACK_CLIENT_ERRORS:
                if code in _PERSISTENT_UNAVAILABLE_ERRORS:
                    _UNAVAILABLE_MODEL_CACHE.add(params["modelId"])
                logger.info("Bedrock %s failed (%s); falling back to profile %s", params["modelId"], code, profile_id)
                params["modelId"] = profile_id
                resp = boto.converse(**params)
            else:
                raise

        is_json = getattr(config, "response_mime_type", None) == "application/json"
        for attempt in range(2):
            output_msg = resp.get("output", {}).get("message", {})
            raw_text = "".join(c.get("text", "") for c in output_msg.get("content", []))
            clean_text = clean_json_fences(raw_text) if is_json or raw_text.strip().startswith("```") else raw_text
            if not is_json:
                break
            try:
                json.loads(clean_text)
                malformed = False
            except ValueError:
                malformed = True
            truncated = resp.get("stopReason") == "max_tokens"
            if not malformed and not truncated:
                break
            if attempt == 1:
                if truncated:
                    raise ValueError("Bedrock JSON response exceeded its output token limit")
                break
            current_budget = params["inferenceConfig"]["maxTokens"]
            params["inferenceConfig"]["maxTokens"] = max(current_budget, min(8192, max(4096, current_budget * 2)))
            logger.warning("Retrying incomplete or malformed Bedrock JSON response (stop_reason=%s, max_tokens=%s)",
                           resp.get("stopReason"), params["inferenceConfig"]["maxTokens"])
            resp = boto.converse(**params)
        usage = resp.get("usage", {})

        parsed = None
        if is_json:
            try:
                parsed = json.loads(clean_text)
                schema = getattr(config, "response_schema", None)
                if schema and hasattr(schema, "model_validate"):
                    parsed = schema.model_validate(parsed)
            except Exception:
                pass

        return BedrockResponse(clean_text, usage, parsed, resp.get("stopReason"))


class BedrockStreamIterator:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            if "contentBlockDelta" in event:
                delta_text = event["contentBlockDelta"].get("delta", {}).get("text", "")
                if delta_text:
                    yield BedrockStreamChunk(delta_text)
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                yield BedrockStreamChunk("", usage=usage)


class BedrockAioModels:
    """Mimics client.aio.models for streaming calls."""

    def __init__(self, bedrock_client: "BedrockClientAdapter"):
        self._adapter = bedrock_client

    async def generate_content_stream(
        self,
        *,
        model: Optional[str] = None,
        contents: Any = None,
        config: Optional[Any] = None,
    ):
        boto = self._adapter.get_boto_client()
        params = _build_converse_params(model, contents, config)
        profile_id = get_bedrock_profile_id()

        loop = asyncio.get_running_loop()
        stream_timeout = float(os.getenv("BEDROCK_STREAM_TIMEOUT_SEC", "120"))
        try:
            stream_resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: boto.converse_stream(**params)),
                timeout=stream_timeout,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if profile_id and params["modelId"] != profile_id and code in _FALLBACK_CLIENT_ERRORS:
                logger.info("Bedrock stream %s failed (%s); falling back to profile %s", params["modelId"], code, profile_id)
                params["modelId"] = profile_id
                stream_resp = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: boto.converse_stream(**params)),
                    timeout=stream_timeout,
                )
            else:
                raise
        events = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: list(stream_resp["stream"])),
            timeout=stream_timeout,
        )
        return BedrockStreamIterator(events)


class BedrockAio:
    def __init__(self, bedrock_client: "BedrockClientAdapter"):
        self.models = BedrockAioModels(bedrock_client)


class BedrockClientAdapter:
    """Adapter implementing the client interface used by services calling Gemini."""

    def __init__(self):
        self._boto_client = None
        self.models = BedrockModels(self)
        self.aio = BedrockAio(self)

    def get_boto_client(self):
        if self._boto_client is None:
            key = get_bedrock_key()
            if key:
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = key
            region = get_bedrock_region()
            read_timeout = max(5, int(os.getenv("BEDROCK_READ_TIMEOUT_SEC", "90")))
            kwargs: Dict[str, Any] = {
                "config": Config(connect_timeout=10, read_timeout=read_timeout, retries={"max_attempts": 1, "mode": "standard"}),
            }
            if region:
                kwargs["region_name"] = region
            self._boto_client = boto3.client("bedrock-runtime", **kwargs)
        return self._boto_client


_bedrock_client_singleton: Optional[BedrockClientAdapter] = None


def get_bedrock_client() -> BedrockClientAdapter:
    global _bedrock_client_singleton
    if _bedrock_client_singleton is None:
        _bedrock_client_singleton = BedrockClientAdapter()
    return _bedrock_client_singleton


def get_ai_client() -> Any:
    """Return Bedrock client if enabled, else None."""
    return get_bedrock_client() if is_bedrock_enabled() else None
