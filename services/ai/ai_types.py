"""Native AI request/content DTOs replacing google.genai.types for AWS Bedrock."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Blob(BaseModel):
    data: bytes = b""
    mime_type: str = ""


class FunctionCall(BaseModel):
    name: str = ""
    args: Optional[Dict[str, Any]] = None


class FunctionResponse(BaseModel):
    name: str = ""
    response: Optional[Dict[str, Any]] = None


class FileData(BaseModel):
    file_uri: str = ""
    mime_type: str = ""


class Part(BaseModel):
    text: Optional[str] = None
    inline_data: Optional[Blob] = None
    function_call: Optional[FunctionCall] = None
    function_response: Optional[FunctionResponse] = None
    file_data: Optional[FileData] = None
    thought: bool = False

    @classmethod
    def from_bytes(cls, *, data: bytes, mime_type: str) -> "Part":
        return cls(inline_data=Blob(data=data, mime_type=mime_type))

    @classmethod
    def from_text(cls, *, text: str) -> "Part":
        return cls(text=text)

    @classmethod
    def from_function_response(cls, *, name: str, response: Dict[str, Any]) -> "Part":
        return cls(function_response=FunctionResponse(name=name, response=response))

    @classmethod
    def from_uri(cls, *, file_uri: str, mime_type: str = "") -> "Part":
        return cls(file_data=FileData(file_uri=file_uri, mime_type=mime_type))


class Content(BaseModel):
    role: Optional[str] = None
    parts: List[Part] = Field(default_factory=list)


class HttpRetryOptions(BaseModel):
    attempts: Optional[int] = None


class HttpOptions(BaseModel):
    timeout: Optional[float] = None
    retry_options: Optional[HttpRetryOptions] = None


class AutomaticFunctionCallingConfig(BaseModel):
    disable: bool = True


class GenerateContentConfig(BaseModel):
    system_instruction: Optional[Any] = None
    response_mime_type: Optional[str] = None
    response_schema: Optional[Any] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    http_options: Optional[HttpOptions] = None
    tools: Optional[List[Any]] = None
    automatic_function_calling: Optional[AutomaticFunctionCallingConfig] = None


class File(BaseModel):
    name: str = ""
    uri: Optional[str] = None

class Candidate(BaseModel):
    content: Optional[Content] = None


class GenerateContentResponse(BaseModel):
    candidates: List[Candidate] = Field(default_factory=list)
    text: Optional[str] = None
