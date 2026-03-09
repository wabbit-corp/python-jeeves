from __future__ import annotations

import asyncio
import inspect
import io
import logging
import re
import time
import types
import wave
from dataclasses import dataclass, field
from pathlib import Path

import discord
from openai import AsyncOpenAI

from servant.defs import GlobalContext
from typed_json import coerce_str

_LOGGER = logging.getLogger(__name__)

VOICE_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/(?P<code>[\w-]+)",
    re.IGNORECASE,
)

DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 2
DEFAULT_SAMPLE_WIDTH_BYTES = 2

DEFAULT_SILENCE_SECONDS = 1.2
DEFAULT_MAX_SEGMENT_SECONDS = 8.0
DEFAULT_FLUSH_INTERVAL_SECONDS = 0.5
DEFAULT_EMPTY_CHANNEL_SECONDS = 60.0

CONFIG_TRANSCRIPT_DIR = "voice_transcript_dir"
CONFIG_SILENCE_SECONDS = "voice_transcription_silence_seconds"
CONFIG_MAX_SEGMENT_SECONDS = "voice_transcription_max_segment_seconds"
CONFIG_FLUSH_INTERVAL_SECONDS = "voice_transcription_flush_interval_seconds"
CONFIG_EMPTY_CHANNEL_SECONDS = "voice_disconnect_empty_seconds"
CONFIG_MODEL = "voice_transcription_model"

DEFAULT_MODEL = "whisper-1"


def extract_invite_code(text: str) -> str | None:
    if not text:
        return None
    match = VOICE_INVITE_RE.search(text)
    if not match:
        return None
    code = match.group("code").strip()
    return code or None


def _load_voice_recv() -> types.ModuleType | None:
    try:
        from discord.ext import voice_recv

        return voice_recv
    except Exception:
        return None


def _verify_voice_deps() -> bool:
    try:
        import nacl
    except Exception:
        _LOGGER.error("PyNaCl not available; voice receive requires it.")
        return False
    _ = nacl.__name__
    return True


def _resolve_transcript_dir(ctx: GlobalContext) -> Path | None:
    raw = ctx.config.get(CONFIG_TRANSCRIPT_DIR)
    if raw is None:
        return None
    path_str = coerce_str(raw, field=CONFIG_TRANSCRIPT_DIR, allow_empty=False)
    return Path(path_str).expanduser().resolve()


def _coerce_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


@dataclass
class _UserBuffer:
    user_id: int
    user_name: str
    last_audio_ts: float
    pcm_chunks: list[bytes] = field(default_factory=list)
    total_bytes: int = 0

    def append(self, pcm: bytes, now: float) -> None:
        if not pcm:
            return
        self.pcm_chunks.append(pcm)
        self.total_bytes += len(pcm)
        self.last_audio_ts = now

    def duration_seconds(self) -> float:
        bytes_per_second = DEFAULT_SAMPLE_RATE * DEFAULT_CHANNELS * DEFAULT_SAMPLE_WIDTH_BYTES
        if bytes_per_second <= 0:
            return 0.0
        return self.total_bytes / bytes_per_second

    def pop_wav_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(DEFAULT_CHANNELS)
            wav_file.setsampwidth(DEFAULT_SAMPLE_WIDTH_BYTES)
            wav_file.setframerate(DEFAULT_SAMPLE_RATE)
            for chunk in self.pcm_chunks:
                wav_file.writeframes(chunk)
        return buffer.getvalue()


class VoiceTranscriber:
    def __init__(
        self,
        *,
        ctx: GlobalContext,
        openai_client: AsyncOpenAI,
        loop: asyncio.AbstractEventLoop,
        transcript_dir: Path | None,
        silence_seconds: float,
        max_segment_seconds: float,
        flush_interval_seconds: float,
        model: str,
    ) -> None:
        self._ctx = ctx
        self._openai_client = openai_client
        self._loop = loop
        self._transcript_dir = transcript_dir
        self._silence_seconds = silence_seconds
        self._max_segment_seconds = max_segment_seconds
        self._flush_interval_seconds = flush_interval_seconds
        self._model = model
        self._buffers: dict[int, _UserBuffer] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self._channel_id: int | None = None
        self._channel_name: str | None = None
        self._guild_id: int | None = None

    def attach_context(self, *, channel: discord.abc.GuildChannel | None) -> None:
        if channel is None:
            return
        self._channel_id = channel.id
        self._channel_name = getattr(channel, "name", None)
        self._guild_id = getattr(channel.guild, "id", None)

    def start(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = self._loop.create_task(self._flush_loop())

    def stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()

    def enqueue_audio(self, user: discord.abc.User, pcm: bytes) -> None:
        if not pcm:
            return

        def _handle() -> None:
            now = time.monotonic()
            raw_id = getattr(user, "id", None)
            if raw_id is None:
                return
            user_id = int(raw_id)
            user_name = getattr(user, "display_name", None) or getattr(user, "name", None) or str(raw_id)
            buffer = self._buffers.get(user_id)
            if buffer is None:
                buffer = _UserBuffer(user_id=user_id, user_name=user_name, last_audio_ts=now)
                self._buffers[user_id] = buffer
            buffer.append(pcm, now)
            if buffer.duration_seconds() >= self._max_segment_seconds:
                self._loop.create_task(self._flush_user(user_id))

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self._loop:
            _handle()
        else:
            self._loop.call_soon_threadsafe(_handle)

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._flush_interval_seconds)
                now = time.monotonic()
                for user_id, buffer in list(self._buffers.items()):
                    if now - buffer.last_audio_ts >= self._silence_seconds:
                        await self._flush_user(user_id)
        except asyncio.CancelledError:
            return

    async def _flush_user(self, user_id: int) -> None:
        buffer = self._buffers.pop(user_id, None)
        if buffer is None or not buffer.pcm_chunks:
            return
        wav_bytes = buffer.pop_wav_bytes()
        await self._transcribe_and_log(buffer, wav_bytes)

    async def flush_all(self) -> None:
        for user_id in list(self._buffers.keys()):
            await self._flush_user(user_id)

    async def _transcribe_and_log(self, buffer: _UserBuffer, wav_bytes: bytes) -> None:
        if not wav_bytes:
            return
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "voice.wav"
        try:
            result = await self._openai_client.audio.transcriptions.create(
                model=self._model,
                file=audio_file,
            )
        except Exception:
            _LOGGER.error("Voice transcription failed for user %s", buffer.user_id, exc_info=True)
            return
        text = getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            return
        log_line = self._format_log_line(buffer, text.strip())
        _LOGGER.info(log_line)
        await self._append_transcript(log_line)

    def _format_log_line(self, buffer: _UserBuffer, text: str) -> str:
        guild = str(self._guild_id) if self._guild_id is not None else "unknown"
        channel = str(self._channel_id) if self._channel_id is not None else "unknown"
        channel_name = self._channel_name or "unknown"
        return (
            f"Voice transcript guild={guild} channel={channel} channel_name={channel_name} "
            f"user={buffer.user_name} user_id={buffer.user_id} text={text}"
        )

    async def _append_transcript(self, line: str) -> None:
        if self._transcript_dir is None:
            return
        path = self._transcript_dir / time.strftime("%Y-%m-%d") / "voice_transcripts.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._append_line_sync, path, line)
        except Exception:
            _LOGGER.error("Failed to append transcript log to %s", path, exc_info=True)

    @staticmethod
    def _append_line_sync(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _extract_pcm_bytes(data: object) -> bytes | None:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    for attr in ("pcm", "data", "raw"):
        raw = getattr(data, attr, None)
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
    return None


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        await result


def _make_sink(voice_recv: types.ModuleType, transcriber: VoiceTranscriber) -> object:
    base = getattr(voice_recv, "AudioSink", object)

    def _write(self: object, user: discord.abc.User, data: object) -> None:
        pcm = _extract_pcm_bytes(data)
        if pcm is None:
            return
        transcriber.enqueue_audio(user, pcm)

    def _cleanup(self: object) -> None:
        transcriber.stop()

    def _wants_opus(self: object) -> bool:
        return False

    sink_cls = type(
        "WhisperSink",
        (base,),
        {
            "write": _write,
            "cleanup": _cleanup,
            "wants_opus": _wants_opus,
        },
    )
    return sink_cls()


class VoiceTranscriberManager:
    def __init__(self, ctx: GlobalContext) -> None:
        self._ctx = ctx
        self._transcribers: dict[int, VoiceTranscriber] = {}
        self._disconnect_tasks: dict[int, asyncio.Task[None]] = {}

    def _build_transcriber(self, openai_client: AsyncOpenAI, loop: asyncio.AbstractEventLoop) -> VoiceTranscriber:
        transcript_dir = _resolve_transcript_dir(self._ctx)
        silence_seconds = _coerce_float(self._ctx.config.get(CONFIG_SILENCE_SECONDS), default=DEFAULT_SILENCE_SECONDS)
        max_segment_seconds = _coerce_float(
            self._ctx.config.get(CONFIG_MAX_SEGMENT_SECONDS), default=DEFAULT_MAX_SEGMENT_SECONDS
        )
        flush_interval_seconds = _coerce_float(
            self._ctx.config.get(CONFIG_FLUSH_INTERVAL_SECONDS), default=DEFAULT_FLUSH_INTERVAL_SECONDS
        )
        model_raw = self._ctx.config.get(CONFIG_MODEL)
        model = coerce_str(model_raw, field=CONFIG_MODEL, allow_empty=False) if model_raw is not None else DEFAULT_MODEL
        return VoiceTranscriber(
            ctx=self._ctx,
            openai_client=openai_client,
            loop=loop,
            transcript_dir=transcript_dir,
            silence_seconds=silence_seconds,
            max_segment_seconds=max_segment_seconds,
            flush_interval_seconds=flush_interval_seconds,
            model=model,
        )

    def _start_disconnect_watch(
        self,
        *,
        guild_id: int,
        voice_client: discord.VoiceProtocol,
        transcriber: VoiceTranscriber,
        empty_seconds: float,
    ) -> None:
        existing = self._disconnect_tasks.get(guild_id)
        if existing is not None:
            existing.cancel()

        async def _watch() -> None:
            empty_since: float | None = None
            try:
                while True:
                    await asyncio.sleep(5.0)
                    channel = getattr(voice_client, "channel", None)
                    if channel is None:
                        return
                    members = getattr(channel, "members", None)
                    if not isinstance(members, list):
                        empty_since = None
                        continue
                    has_humans = any(not getattr(member, "bot", False) for member in members)
                    now = time.monotonic()
                    if has_humans:
                        empty_since = None
                        continue
                    if empty_since is None:
                        empty_since = now
                        continue
                    if (now - empty_since) >= empty_seconds:
                        await transcriber.flush_all()
                        transcriber.stop()
                        disconnect = getattr(voice_client, "disconnect", None)
                        if callable(disconnect):
                            try:
                                await _maybe_await(disconnect(force=True))
                            except TypeError:
                                await _maybe_await(disconnect())
                        return
            except asyncio.CancelledError:
                return

        loop = self._ctx.discord_loop
        if loop is None:
            raise RuntimeError("Discord loop not initialized.")
        self._disconnect_tasks[guild_id] = loop.create_task(_watch())

    async def join_invite(
        self,
        *,
        client: discord.Client,
        invite: discord.Invite,
    ) -> None:
        channel = invite.channel
        if channel is None:
            _LOGGER.warning("Invite %s did not resolve to a channel.", invite.code)
            return

        voice_recv = _load_voice_recv()
        if voice_recv is None:
            _LOGGER.error("discord.ext.voice_recv is not available; cannot receive voice audio.")
            return
        if not _verify_voice_deps():
            return
        voice_client_cls = getattr(voice_recv, "VoiceRecvClient", None)
        if not isinstance(voice_client_cls, type):
            _LOGGER.error("discord.ext.voice_recv.VoiceRecvClient missing; cannot receive voice audio.")
            return

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            _LOGGER.info("Invite %s is not a voice channel invite (channel=%s).", invite.code, channel)
            return

        openai_client = self._ctx.openai_client
        if openai_client is None:
            raise RuntimeError("OpenAI client not initialized.")

        loop = self._ctx.discord_loop
        if loop is None:
            raise RuntimeError("Discord loop not initialized.")

        guild_id = channel.guild.id
        transcriber = self._transcribers.get(guild_id)
        if transcriber is None:
            transcriber = self._build_transcriber(openai_client, loop)
            self._transcribers[guild_id] = transcriber
        empty_seconds = _coerce_float(
            self._ctx.config.get(CONFIG_EMPTY_CHANNEL_SECONDS),
            default=DEFAULT_EMPTY_CHANNEL_SECONDS,
        )

        voice_client = discord.utils.get(client.voice_clients, guild=channel.guild)
        if voice_client is not None:
            is_connected = getattr(voice_client, "is_connected", None)
            connected = bool(is_connected()) if callable(is_connected) else True
            if connected:
                if not callable(getattr(voice_client, "listen", None)):
                    disconnect = getattr(voice_client, "disconnect", None)
                    if callable(disconnect):
                        try:
                            await _maybe_await(disconnect(force=True))
                        except TypeError:
                            await _maybe_await(disconnect())
                    voice_client = None
                else:
                    if getattr(voice_client, "channel", None) != channel:
                        move_to = getattr(voice_client, "move_to", None)
                        if callable(move_to):
                            await _maybe_await(move_to(channel))
        if voice_client is None:
            voice_client = await channel.connect(cls=voice_client_cls, self_deaf=False, self_mute=True)

        transcriber.attach_context(channel=channel)
        transcriber.start()

        sink = _make_sink(voice_recv, transcriber)
        listen = getattr(voice_client, "listen", None)
        if callable(listen):
            listen(sink)
        self._start_disconnect_watch(
            guild_id=guild_id,
            voice_client=voice_client,
            transcriber=transcriber,
            empty_seconds=empty_seconds,
        )
        _LOGGER.info(
            "Voice transcription active: guild=%s channel=%s invite=%s",
            channel.guild.id,
            channel.id,
            invite.code,
        )


def _manager(ctx: GlobalContext) -> VoiceTranscriberManager:
    state = ctx.module_state.get("voice_transcriber")
    if isinstance(state, VoiceTranscriberManager):
        return state
    manager = VoiceTranscriberManager(ctx)
    ctx.module_state["voice_transcriber"] = manager
    return manager


async def maybe_handle_voice_invite(ctx: GlobalContext, client: discord.Client, message: discord.Message) -> bool:
    code = extract_invite_code(message.content or "")
    if not code:
        return False

    try:
        invite = await client.fetch_invite(code, with_counts=False)
    except Exception:
        _LOGGER.error("Failed to fetch invite %s", code, exc_info=True)
        return False

    manager = _manager(ctx)
    await manager.join_invite(client=client, invite=invite)
    return True
