from __future__ import annotations

"""
almi WebRTC Voice Client
Two-device, full-duplex, always-on voice channel over WebRTC.

Usage:
    DEVICE_ROLE=caller  python3 client.py
    DEVICE_ROLE=answerer python3 client.py
"""

import asyncio
import fractions
import json
import logging
import os
import platform
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from pyaec import Aec
from av import AudioFrame
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import AudioStreamTrack, MediaStreamError
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def determine_role() -> str:
    role = os.environ.get("DEVICE_ROLE", "").lower()
    if role in ("caller", "answerer"):
        return role
    raise ValueError(
        "Set DEVICE_ROLE environment variable to 'caller' or 'answerer'.\n"
        f"  Current hostname: {platform.node()}"
    )


# ---------------------------------------------------------------------------
# EchoCancellerBridge — shared AEC between MicrophoneTrack and AudioSink
# ---------------------------------------------------------------------------

class EchoCancellerBridge:
    """Thread-safe bridge that feeds playback reference and cancels echo from mic."""

    def __init__(self, frame_size: int, sample_rate: int, filter_length_ms: int = 200):
        filter_length = int(sample_rate * filter_length_ms / 1000)
        self._aec = Aec(frame_size, filter_length, sample_rate)
        self._playback_ref: bytes | None = None
        self._lock = threading.Lock()
        log.info(
            "AEC initialised: frame_size=%d, filter_length=%d (~%dms)",
            frame_size, filter_length, filter_length_ms,
        )

    def feed_playback(self, pcm_int16: np.ndarray):
        """Called by AudioSink with each frame written to speakers."""
        mono = pcm_int16[:, 0] if pcm_int16.ndim == 2 else pcm_int16
        with self._lock:
            self._playback_ref = mono.astype(np.int16).tobytes()

    def cancel(self, mic_int16: np.ndarray) -> np.ndarray:
        """Called by MicrophoneTrack to remove echo from captured audio."""
        mono = mic_int16[:, 0] if mic_int16.ndim == 2 else mic_int16
        mic_bytes = mono.astype(np.int16).tobytes()
        with self._lock:
            ref = self._playback_ref
        if ref is None:
            return mic_int16
        out_bytes = self._aec.cancel_echo(mic_bytes, ref)
        out = np.frombuffer(out_bytes, dtype=np.int16)
        if mic_int16.ndim == 2:
            return out.reshape(-1, 1)
        return out


# ---------------------------------------------------------------------------
# MicrophoneTrack — bridges sounddevice callback thread → aiortc async world
# ---------------------------------------------------------------------------

class MicrophoneTrack(AudioStreamTrack):
    """Captures from the system default microphone and feeds aiortc Opus encoder."""

    kind = "audio"

    def __init__(self, audio_cfg: dict, echo_canceller: EchoCancellerBridge | None = None):
        super().__init__()
        self._sample_rate: int = audio_cfg["sampleRate"]
        self._channels: int = audio_cfg["channels"]
        self._frame_size: int = audio_cfg["frameSize"]
        self._latency = audio_cfg.get("latency", "low")
        self._aec = echo_canceller

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._loop = asyncio.get_event_loop()
        self._timestamp: int = 0
        self._start: float | None = None

        def _sd_callback(indata: np.ndarray, frames: int, time_info, status):
            # Runs in a C audio thread — must NOT touch asyncio directly.
            if status:
                log.warning("sounddevice input status: %s", status)
            data = indata.copy()  # copy before buffer is reused

            def _enqueue():
                try:
                    self._queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass  # drop stale frame; keep latency low

            self._loop.call_soon_threadsafe(_enqueue)

        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="int16",
            blocksize=self._frame_size,
            latency=self._latency,
            callback=_sd_callback,
        )
        self._stream.start()
        log.info(
            "MicrophoneTrack started: %dHz, %dch, %d-sample frames",
            self._sample_rate, self._channels, self._frame_size,
        )

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        # On the first recv(), discard any frames that accumulated during
        # connection setup to avoid delivering stale (delayed) audio.
        if self._start is None:
            while self._queue.qsize() > 0:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        data = await self._queue.get()  # shape: (frame_size, channels)

        # Apply acoustic echo cancellation if available.
        if self._aec is not None:
            data = self._aec.cancel(data)

        # Real-time pacing: sleep until this frame's expected wall-clock time.
        if self._start is None:
            self._start = time.monotonic()
            self._timestamp = 0
        else:
            self._timestamp += self._frame_size
            expected = self._start + self._timestamp / self._sample_rate
            wait = expected - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

        # PyAV AudioFrame.from_ndarray expects (channels, samples) for s16.
        # sounddevice gives us (samples, channels), so we transpose.
        frame = AudioFrame.from_ndarray(
            data.T,  # (channels, samples)
            format="s16",
            layout="mono" if self._channels == 1 else "stereo",
        )
        frame.pts = self._timestamp
        frame.sample_rate = self._sample_rate
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        return frame

    def stop(self):
        self._stream.stop()
        self._stream.close()
        super().stop()
        log.info("MicrophoneTrack stopped")


# ---------------------------------------------------------------------------
# AudioSink — consumes a remote AudioStreamTrack and plays it to the speaker
# ---------------------------------------------------------------------------

class AudioSink:
    """Writes decoded Opus frames from the remote peer to the system speaker."""

    def __init__(self, audio_cfg: dict, echo_canceller: EchoCancellerBridge | None = None):
        self._sample_rate: int = audio_cfg["sampleRate"]
        self._channels: int = audio_cfg["channels"]
        self._frame_size: int = audio_cfg["frameSize"]
        self._latency = audio_cfg.get("latency", "low")
        self._aec = echo_canceller
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="int16",
            blocksize=self._frame_size,
            latency=self._latency,
        )
        self._stream.start()
        log.info(
            "AudioSink started: %dHz, %dch, %d-sample frames",
            self._sample_rate, self._channels, self._frame_size,
        )

    async def consume(self, track: AudioStreamTrack):
        """Pull frames from remote track and write to speaker. Runs until track ends."""
        loop = asyncio.get_event_loop()
        first = True
        try:
            while True:
                frame = await track.recv()
                data = frame.to_ndarray()
                fmt = frame.format.name
                n_ch = len(frame.layout.channels)

                if first:
                    log.info(
                        "FRAME DEBUG — fmt=%s layout=%s n_ch=%d "
                        "sample_rate=%d samples=%d raw_shape=%s",
                        fmt, frame.layout.name, n_ch,
                        frame.sample_rate, frame.samples, data.shape,
                    )
                    first = False

                if fmt == "s16":
                    # Packed interleaved: [L0,R0,L1,R1,...] → (channels, samples).
                    # reshape(n_ch,-1) would split the flat array into n_ch blocks,
                    # NOT de-interleave. Correct way: reshape(-1, n_ch) then transpose.
                    data = data[0].reshape(-1, n_ch).T.astype(np.int16)
                elif fmt == "fltp":
                    # Float planar [-1.0, 1.0] → int16, shape (channels, samples).
                    data = (data * 32767).clip(-32768, 32767).astype(np.int16)
                else:
                    # s16p or other planar int formats: already (channels, samples).
                    data = data.astype(np.int16)

                # Downmix to mono if needed.
                if data.shape[0] > 1:
                    data = data.mean(axis=0, keepdims=True).astype(np.int16)
                # Run the blocking write in a thread so it never stalls the
                # event loop (which would prevent STUN consent-refresh responses).
                pcm = data.T.copy()  # (samples, channels); copy for thread safety
                if self._aec is not None:
                    self._aec.feed_playback(pcm)
                await loop.run_in_executor(None, self._stream.write, pcm)
        except Exception as exc:
            log.info("AudioSink.consume ended: %s", exc)
        finally:
            self._stream.stop()
            self._stream.close()
            log.info("AudioSink stopped")


# ---------------------------------------------------------------------------
# ICE / signaling helpers
# ---------------------------------------------------------------------------

async def _wait_for_ice_complete(pc: RTCPeerConnection, timeout: float = 30.0):
    """Wait until all ICE candidates have been gathered (non-trickle strategy)."""
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_state():
        log.debug("ICE gathering state: %s", pc.iceGatheringState)
        if pc.iceGatheringState == "complete":
            done.set()

    await asyncio.wait_for(done.wait(), timeout=timeout)


async def _run_caller(pc: RTCPeerConnection, ws, config: dict):
    """Create an offer, wait for full ICE gathering, send it, wait for answer."""
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    log.info("ICE gathering started (caller)…")
    await _wait_for_ice_complete(pc)

    await ws.send(json.dumps({
        "type": "offer",
        "room": config["roomId"],
        "sdp": pc.localDescription.sdp,
        "sdpType": pc.localDescription.type,
    }))
    log.info("Offer sent, waiting for answer…")

    raw = await ws.recv()
    msg = json.loads(raw)
    if msg.get("type") != "answer":
        raise RuntimeError(f"Expected 'answer', got: {msg.get('type')}")
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=msg["sdp"], type=msg["sdpType"])
    )
    log.info("Answer received and applied")


async def _run_answerer(pc: RTCPeerConnection, ws, config: dict):
    """Wait for an offer, answer it, then wait for full ICE gathering and send."""
    log.info("Waiting for offer…")
    raw = await ws.recv()
    msg = json.loads(raw)
    if msg.get("type") != "offer":
        raise RuntimeError(f"Expected 'offer', got: {msg.get('type')}")
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=msg["sdp"], type=msg["sdpType"])
    )
    log.info("Offer received, creating answer…")

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    log.info("ICE gathering started (answerer)…")
    await _wait_for_ice_complete(pc)

    await ws.send(json.dumps({
        "type": "answer",
        "room": config["roomId"],
        "sdp": pc.localDescription.sdp,
        "sdpType": pc.localDescription.type,
    }))
    log.info("Answer sent")


# ---------------------------------------------------------------------------
# Ring tone
# ---------------------------------------------------------------------------

async def _play_ring(sample_rate: int = 48000, rings: int = 2):
    """
    Play a telephone ring tone (440 Hz + 480 Hz, classic NA ring).
    Each ring = 2 s on / 0.5 s off.  Runs in a thread executor so the
    event loop stays responsive during playback.
    """
    loop = asyncio.get_event_loop()
    on_samples  = int(sample_rate * 2.0)
    off_samples = int(sample_rate * 0.5)
    t = np.linspace(0, 2.0, on_samples, endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t)) / 2
    pcm_on  = (tone * 16000).astype(np.int16)
    pcm_off = np.zeros(off_samples, dtype=np.int16)

    def _play():
        for _ in range(rings):
            sd.play(pcm_on,  sample_rate)
            sd.wait()
            sd.play(pcm_off, sample_rate)
            sd.wait()

    log.info("Ring!")
    await loop.run_in_executor(None, _play)


# ---------------------------------------------------------------------------
# Single connection attempt
# ---------------------------------------------------------------------------

async def _connect_and_run(config: dict, role: str):
    """
    One full lifecycle: open WebSocket → negotiate WebRTC → stream audio.
    Creates a fresh RTCPeerConnection and MicrophoneTrack every call so that
    a failed PC is never reused.
    """
    ice_servers = [RTCIceServer(urls=s["urls"]) for s in config["stunServers"]]
    rtc_config = RTCConfiguration(iceServers=ice_servers)
    audio_cfg = config["audio"]

    aec = EchoCancellerBridge(
        frame_size=audio_cfg["frameSize"],
        sample_rate=audio_cfg["sampleRate"],
    )
    mic = MicrophoneTrack(audio_cfg, echo_canceller=aec)
    sink = AudioSink(audio_cfg, echo_canceller=aec)
    pc = RTCPeerConnection(configuration=rtc_config)
    pc.addTrack(mic)

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            log.info("Remote audio track received — starting playback")
            asyncio.ensure_future(sink.consume(track))

    @pc.on("connectionstatechange")
    async def on_connection_state():
        log.info("WebRTC connection state: %s", pc.connectionState)
        if pc.connectionState == "connected":
            asyncio.ensure_future(_play_ring(audio_cfg["sampleRate"]))
        elif pc.connectionState in ("failed", "closed"):
            await pc.close()

    try:
        async with websockets.connect(
            config["signalingUrl"],
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"type": "join", "room": config["roomId"]}))
            log.info("Joined room '%s' as %s", config["roomId"], role)

            if role == "caller":
                await _run_caller(pc, ws, config)
            else:
                await _run_answerer(pc, ws, config)

            log.info("WebRTC negotiation complete — audio channel is live")

            # Hold here until the connection fails or the WebSocket drops.
            await asyncio.Future()

    finally:
        mic.stop()
        await pc.close()


# ---------------------------------------------------------------------------
# Main loop with exponential backoff reconnection
# ---------------------------------------------------------------------------

async def main():
    config = load_config()
    role = determine_role()
    log.info("Starting almi voice client — role: %s", role)

    backoff = 1.0
    max_backoff = 60.0

    while True:
        try:
            log.info("Connecting to %s…", config["signalingUrl"])
            await _connect_and_run(config, role)
        except (ConnectionClosed, WebSocketException, OSError, asyncio.TimeoutError) as exc:
            log.warning("Connection lost (%s). Retrying in %.0fs…", exc, backoff)
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
        else:
            backoff = 1.0  # clean exit — reset backoff
            continue

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    asyncio.run(main())
