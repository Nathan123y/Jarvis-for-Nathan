"""A thin adapter over a Gemini Live session for phone calls.

The call code talks to this, not to the SDK, so tests can swap in a scripted
fake and an SDK change touches one file. Events come out as plain tuples:
    ("audio", pcm24_bytes)      the assistant speaking, 24 kHz 16-bit PCM
    ("interrupted",)            the caller talked over it: drop queued audio
    ("heard", text)             transcription of the caller
    ("said", text)              transcription of the assistant
    ("tool", id, name, args)    the assistant wants a tool run
"""
from __future__ import annotations

import contextlib
from typing import AsyncIterator

LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
VOICE = "Aoede"


class GeminiCall:
    def __init__(self, session):
        self._session = session

    async def send_audio(self, pcm16k: bytes) -> None:
        from google.genai import types
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000"))

    async def send_text(self, text: str) -> None:
        await self._session.send_client_content(
            turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)

    async def send_tool_result(self, call_id: str, name: str, result: str) -> None:
        from google.genai import types
        await self._session.send_tool_response(function_responses=[
            types.FunctionResponse(id=call_id, name=name, response={"result": result})])

    async def events(self) -> AsyncIterator[tuple]:
        # receive() stops at the end of each model turn; a call has many.
        while True:
            async for msg in self._session.receive():
                if msg.data:
                    yield ("audio", msg.data)
                sc = msg.server_content
                if sc is not None:
                    if sc.interrupted:
                        yield ("interrupted",)
                    if sc.input_transcription and sc.input_transcription.text:
                        yield ("heard", sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        yield ("said", sc.output_transcription.text)
                if msg.tool_call:
                    for fc in msg.tool_call.function_calls or []:
                        yield ("tool", fc.id, fc.name, dict(fc.args or {}))


@contextlib.asynccontextmanager
async def connect(api_key: str, system: str, tools: list, voice: str = VOICE,
                  model: str = LIVE_MODEL):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription={},
        output_audio_transcription={},
        system_instruction=system,
        tools=[{"function_declarations": tools}],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
    )
    async with client.aio.live.connect(model=model, config=config) as session:
        yield GeminiCall(session)
