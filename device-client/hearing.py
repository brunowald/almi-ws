"""
almi hearing — always-on call listener.

Run on startup. When the other device calls, the phone rings.
Press y to answer or n to reject.

Usage:
    python3 hearing.py
"""

import asyncio
import json
import logging

import numpy as np
import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from client import MicrophoneTrack, AudioSink, load_config, _wait_for_ice_complete

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
    t = np.linspace(0, 2.0, on_samples, endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 480 * t)) / 2
    pcm = (tone * 16000).astype(np.int16)
    try:
        while True:
            sd.play(pcm, sample_rate)
            await loop.run_in_executor(None, sd.wait)
            await asyncio.sleep(4.0)
    except asyncio.CancelledError:
        sd.stop()
        raise


# ---------------------------------------------------------------------------
# User prompt
# ---------------------------------------------------------------------------

async def _ask_user() -> bool:
    """Ask y/n in a thread executor so the event loop stays responsive."""
    loop = asyncio.get_event_loop()
    while True:
        ans = await loop.run_in_executor(
            None, input, "\nIncoming call — answer? (y/n): "
        )
        if ans.strip().lower() in ("y", "n"):
            return ans.strip().lower() == "y"


# ---------------------------------------------------------------------------
# Answerer WebRTC flow
# ---------------------------------------------------------------------------

async def _handle_call(ws, config: dict):
    """Receive offer → answer → stream audio."""
    audio_cfg = config["audio"]
    ice_servers = [RTCIceServer(urls=s["urls"]) for s in config["stunServers"]]
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    mic = MicrophoneTrack(audio_cfg)
    sink = AudioSink(audio_cfg)
    pc.addTrack(mic)

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            log.info("Remote audio track received — starting playback")
            asyncio.ensure_future(sink.consume(track))

    @pc.on("connectionstatechange")
    async def on_connection_state():
        log.info("WebRTC connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()

    try:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "offer":
            raise RuntimeError(f"Expected 'offer', got: {msg.get('type')}")

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=msg["sdp"], type=msg["sdpType"])
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
        log.info("Call connected — audio channel is live")
        await asyncio.Future()  # hold until connection drops or WS closes
    finally:
        mic.stop()
        await pc.close()


# ---------------------------------------------------------------------------
# Main session loop
# ---------------------------------------------------------------------------

async def _session(config: dict):
    async with websockets.connect(
        config["signalingUrl"], ping_interval=20, ping_timeout=10
    ) as ws:
        await ws.send(json.dumps({"type": "join", "room": config["roomId"]}))
        log.info("Listening for calls in room '%s'…", config["roomId"])

        while True:
            raw = await ws.recv()
            msg = json.loads(raw)

            if msg.get("type") != "call":
                continue

            log.info("Incoming call!")

            ring_task = asyncio.ensure_future(
                _ring_loop(config["audio"]["sampleRate"])
            )
            accepted = await _ask_user()
            ring_task.cancel()
            try:
                await ring_task
            except asyncio.CancelledError:
                pass

            if not accepted:
                log.info("Call rejected")
                await ws.send(json.dumps({"type": "reject", "room": config["roomId"]}))
                continue

            await ws.send(json.dumps({"type": "accept", "room": config["roomId"]}))
            log.info("Call accepted — waiting for offer…")
            await _handle_call(ws, config)
            log.info("Call ended — back to listening")


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
    asyncio.run(main())
