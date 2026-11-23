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
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


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
    """Return True if either AWS Bedrock or Google Gemini is configured."""
    return is_bedrock_enabled() or bool(os.getenv("GEMINI_API_KEY", "").strip())


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

    def __init__(self, text: str, usage: Optional[Dict[str, Any]] = None, parsed: Any = None):
        self._text = text
        self.candidates = [BedrockCandidate(text)]
        in_tok = (usage or {}).get("inputTokens", 0)
        out_tok = (usage or {}).get("outputTokens", 0)
        self.usage_metadata = BedrockUsageMetadata(in_tok, out_tok)
        self.parsed = parsed

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
    profile_id = get_bedrock_profile_id()
    model_id = profile_id if (not model or "gemini" in model.lower()) else model

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
                schema_str = json.dumps(response_schema)
                json_instruction = f"Return ONLY valid JSON matching this schema: {schema_str}. Do not include markdown code block syntax (no ```json)."
            except Exception:
                json_instruction = "Return ONLY valid JSON. Do not include markdown code block syntax (no ```json)."
        elif isinstance(response_schema, str) and response_schema.strip():
            json_instruction = f"Return ONLY valid JSON matching this schema: {response_schema}. Do not include markdown code block syntax (no ```json)."
        else:
            json_instruction = "Return ONLY valid JSON. Do not include markdown code block syntax (no ```json)."
        system_text = f"{system_text}\n{json_instruction}".strip() if system_text else json_instruction

    system = [{"text": system_text}] if system_text else None

    content_blocks, _ = _extract_text_and_media(contents)
    messages = [{"role": "user", "content": content_blocks}]

    max_tokens = getattr(config, "max_output_tokens", None) or 4096
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
        resp = boto.converse(**params)

        raw_text = ""
        output_msg = resp.get("output", {}).get("message", {})
        for c in output_msg.get("content", []):
            if "text" in c:
                raw_text += c["text"]

        is_json = getattr(config, "response_mime_type", None) == "application/json"
        clean_text = clean_json_fences(raw_text) if is_json or raw_text.strip().startswith("```") else raw_text
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

        return BedrockResponse(clean_text, usage, parsed)


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

        loop = asyncio.get_running_loop()
        stream_resp = await loop.run_in_executor(None, lambda: boto.converse_stream(**params))
        events = await loop.run_in_executor(None, lambda: list(stream_resp["stream"]))
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
            kwargs: Dict[str, Any] = {}
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
    """Return Bedrock client if enabled; otherwise Gemini client if GEMINI_API_KEY is present."""
    if is_bedrock_enabled():
        return get_bedrock_client()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if api_key:
        from google import genai
        return genai.Client(api_key=api_key)
    return None
