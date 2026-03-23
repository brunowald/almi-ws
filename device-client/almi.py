"""
almi — two-device always-on voice system.

Both devices run this script.
  - Press Enter to call the other device.
  - When a call comes in, the phone rings. Press y (or Enter) to answer, n to reject.
  - Ctrl+C to quit.

Usage:
    python3 almi.py
"""

import asyncio
import json
import logging
import sys

import numpy as np
import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from client import MicrophoneTrack, AudioSink, load_config, _wait_for_ice_complete, _play_ring

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ring tone loop
# ---------------------------------------------------------------------------

async def _ring_loop(sample_rate: int = 48000):
    """Ring continuously (2 s on / 4 s off) until cancelled."""
    loop = asyncio.get_event_loop()
    on_samples = int(sample_rate * 2.0)
    off_samples = int(sample_rate * 4.0)
    t = np.linspace(0, 2.0, on_samples, endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t)) / 2
    pcm_on  = (tone * 16000).astype(np.int16).reshape(-1, 1)
    pcm_off = np.zeros((off_samples, 1), dtype=np.int16)
    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
    stream.start()
    try:
        while True:
            await loop.run_in_executor(None, stream.write, pcm_on)
            await loop.run_in_executor(None, stream.write, pcm_off)
    except asyncio.CancelledError:
        stream.stop()
        stream.close()
        raise


# ---------------------------------------------------------------------------
# WebRTC call flow (caller + answerer unified)
# ---------------------------------------------------------------------------

def _make_pc(config: dict) -> RTCPeerConnection:
    ice_servers = [RTCIceServer(urls=s["urls"]) for s in config["stunServers"]]
    return RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))


async def _run_call(ws, event_queue: asyncio.Queue, config: dict, is_caller: bool):
    """
    Full call lifecycle: setup tracks → negotiate SDP → stream audio.
    Tracks MUST be added before createOffer/createAnswer so they appear in the SDP.
    """
    audio_cfg = config["audio"]
    pc = _make_pc(config)
    mic = MicrophoneTrack(audio_cfg)
    sink = AudioSink(audio_cfg)
    pc.addTrack(mic)          # ← must be before createOffer/createAnswer
    call_done = asyncio.Event()

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            log.info("Remote audio track received — starting playback")
            asyncio.ensure_future(sink.consume(track))

    @pc.on("connectionstatechange")
    async def on_state():
        log.info("WebRTC connection state: %s", pc.connectionState)
        if pc.connectionState == "connected":
            asyncio.ensure_future(_play_ring(audio_cfg["sampleRate"], rings=1))
        elif pc.connectionState in ("failed", "closed"):
            call_done.set()

    try:
        if is_caller:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await _wait_for_ice_complete(pc)
            await ws.send(json.dumps({
                "type": "offer",
                "room": config["roomId"],
                "sdp": pc.localDescription.sdp,
                "sdpType": pc.localDescription.type,
            }))
            log.info("Offer sent, waiting for answer…")
            while True:
                source, data = await event_queue.get()
                if source == "error":
                    raise data
                if source == "ws" and data.get("type") == "answer":
                    break
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
            )
        else:
            while True:
                source, data = await event_queue.get()
                if source == "error":
                    raise data
                if source == "ws" and data.get("type") == "offer":
                    break
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await _wait_for_ice_complete(pc)
            await ws.send(json.dumps({
                "type": "answer",
                "room": config["roomId"],
                "sdp": pc.localDescription.sdp,
                "sdpType": pc.localDescription.type,
            }))

        log.info("Audio channel is live")
        await call_done.wait()
    finally:
        mic.stop()
        await pc.close()
        log.info("Call ended.")


# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------

async def _session(config: dict):
    async with websockets.connect(
        config["signalingUrl"], ping_interval=20, ping_timeout=10
    ) as ws:
        await ws.send(json.dumps({"type": "join", "room": config["roomId"]}))
        print("\nReady. Press Enter to call.\n")

        event_queue: asyncio.Queue = asyncio.Queue()

        async def _ws_pump():
            try:
                async for raw in ws:
                    await event_queue.put(("ws", json.loads(raw)))
            except Exception as exc:
                await event_queue.put(("error", exc))

        async def _kbd_pump():
            loop = asyncio.get_event_loop()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                await event_queue.put(("kbd", line.strip().lower()))

        ws_pump = asyncio.ensure_future(_ws_pump())
        kbd_pump = asyncio.ensure_future(_kbd_pump())

        try:
            while True:
                source, data = await event_queue.get()

                if source == "error":
                    raise data

                # ── Outgoing call ────────────────────────────────────────────
                if source == "kbd":
                    log.info("Calling…")
                    await ws.send(json.dumps({"type": "call", "room": config["roomId"]}))

                    # Wait for accept / reject (ignore other events)
                    while True:
                        src2, dat2 = await event_queue.get()
                        if src2 == "ws" and dat2.get("type") in ("accept", "reject"):
                            break

                    if dat2["type"] == "reject":
                        print("\nCall rejected.\nPress Enter to call.\n")
                        continue

                    log.info("Call accepted — connecting…")
                    await _run_call(ws, event_queue, config, is_caller=True)
                    print("\nPress Enter to call.\n")

                # ── Incoming call ─────────────────────────────────────────────
                elif source == "ws" and data.get("type") == "call":
                    log.info("Incoming call!")

                    ring_task = asyncio.ensure_future(
                        _ring_loop(config["audio"]["sampleRate"])
                    )

                    print("\nIncoming call — answer? (y/n): ", end="", flush=True)

                    # Wait for y/n keyboard input (ignore WS events while prompting)
                    while True:
                        src2, dat2 = await event_queue.get()
                        if src2 == "kbd":
                            break

                    ring_task.cancel()
                    try:
                        await ring_task
                    except asyncio.CancelledError:
                        pass

                    if dat2 in ("y", ""):
                        await ws.send(json.dumps({"type": "accept", "room": config["roomId"]}))
                        log.info("Answering…")
                        await _run_call(ws, event_queue, config, is_caller=False)
                        print("\nPress Enter to call.\n")
                    else:
                        await ws.send(json.dumps({"type": "reject", "room": config["roomId"]}))
                        log.info("Call rejected.")
                        print("\nPress Enter to call.\n")

        finally:
            ws_pump.cancel()
            kbd_pump.cancel()


async def main():
    config = load_config()
    backoff = 1.0
    while True:
        try:
            await _session(config)
        except (ConnectionClosed, WebSocketException, OSError, asyncio.TimeoutError) as exc:
            log.warning("Connection lost (%s). Retrying in %.0fs…", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        else:
            backoff = 1.0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bye.")
