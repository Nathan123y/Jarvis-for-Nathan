"""OpenAI Realtime transport with a Gemini-Live-compatible surface.

Jarvis' audio/UI loop predates OpenAI Realtime and expects four small session
methods.  This adapter keeps that loop provider-agnostic while speaking the
current GA Realtime WebSocket protocol on the wire.
"""
from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from websockets.asyncio.client import connect


def _ns(**values):
    return SimpleNamespace(**values)


def _lower_schema(value):
    if isinstance(value, dict):
        return {k: _lower_schema(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_lower_schema(v) for v in value]
    if isinstance(value, str) and value in {"OBJECT", "ARRAY", "STRING", "NUMBER", "INTEGER", "BOOLEAN"}:
        return value.lower()
    return value


def openai_tools(declarations):
    """Convert Jarvis/Gemini function declarations to Realtime tools."""
    tools = []
    for raw in declarations:
        d = raw if isinstance(raw, dict) else raw.model_dump(exclude_none=True)
        tools.append({
            "type": "function",
            "name": d["name"],
            "description": d.get("description", ""),
            "parameters": _lower_schema(d.get("parameters") or {"type": "object", "properties": {}}),
        })
    return tools


class OpenAIRealtimeSession:
    def __init__(self, websocket):
        self._ws = websocket

    async def _send(self, payload):
        await self._ws.send(json.dumps(payload))

    async def start(self, *, instructions, tools, voice="marin"):
        await self._send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": "gpt-live-transcribe"},
                        "turn_detection": {"type": "server_vad"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": voice,
                    },
                },
                "tools": tools,
                "tool_choice": "auto",
            },
        })

    async def send_realtime_input(self, audio):
        data = getattr(audio, "data", b"")
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(data).decode("ascii"),
        })

    async def send_client_content(self, *, turns, turn_complete=True):
        parts = turns.get("parts", [])
        content = []
        for part in parts:
            if part.get("text"):
                content.append({"type": "input_text", "text": part["text"]})
            inline = part.get("inline_data")
            if inline:
                url = f"data:{inline['mime_type']};base64,{inline['data']}"
                content.append({"type": "input_image", "image_url": url})
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": content},
        })
        if turn_complete:
            await self._send({"type": "response.create"})

    async def send_tool_response(self, *, function_responses):
        for fr in function_responses:
            response = getattr(fr, "response", {}) or {}
            output = response.get("result", response)
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, default=str)
            await self._send({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": getattr(fr, "id", ""),
                    "output": output,
                },
            })
        await self._send({"type": "response.create"})

    async def receive(self):
        """Yield the tiny response shape consumed by JarvisLive."""
        async for raw in self._ws:
            event = json.loads(raw)
            kind = event.get("type", "")
            data = None
            server_content = None
            tool_call = None

            if kind == "response.output_audio.delta":
                data = base64.b64decode(event.get("delta", ""))
            elif kind == "response.output_audio_transcript.delta":
                server_content = _ns(
                    output_transcription=_ns(text=event.get("delta", "")),
                    input_transcription=None, turn_complete=False,
                )
            elif kind == "conversation.item.input_audio_transcription.completed":
                server_content = _ns(
                    output_transcription=None,
                    input_transcription=_ns(text=event.get("transcript", "")),
                    turn_complete=False,
                )
            elif kind == "response.function_call_arguments.done":
                try:
                    args = json.loads(event.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                call = _ns(
                    id=event.get("call_id", ""),
                    name=event.get("name", ""),
                    args=args,
                )
                tool_call = _ns(function_calls=[call])
            elif kind in {"response.done", "response.output_audio.done"}:
                server_content = _ns(
                    output_transcription=None, input_transcription=None,
                    turn_complete=True,
                )
            elif kind == "error":
                detail = event.get("error", {}).get("message", str(event))
                raise RuntimeError(f"OpenAI Realtime error: {detail}")

            if data is not None or server_content is not None or tool_call is not None:
                yield _ns(
                    data=data, server_content=server_content,
                    tool_call=tool_call, session_resumption_update=None,
                )

    async def interrupt(self):
        await self._send({"type": "response.cancel"})
        await self._send({"type": "output_audio_buffer.clear"})


@asynccontextmanager
async def connect_openai(*, api_key, model, instructions, tools, voice="marin"):
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with connect(url, additional_headers=headers, max_size=None) as ws:
        session = OpenAIRealtimeSession(ws)
        await session.start(instructions=instructions, tools=openai_tools(tools), voice=voice)
        yield session
