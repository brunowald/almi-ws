"""
almi calling — initiate a call to a device running hearing.py.

Usage:
    python3 calling.py
"""

import asyncio
import json
import logging

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


async def main():
    config = load_config()
    audio_cfg = config["audio"]

    async with websockets.connect(
        config["signalingUrl"], ping_interval=20, ping_timeout=10
    ) as ws:
        await ws.send(json.dumps({"type": "join", "room": config["roomId"]}))
        await ws.send(json.dumps({"type": "call", "room": config["roomId"]}))
        log.info("Calling… waiting for the other device to answer")

        raw = await ws.recv()
        msg = json.loads(raw)

        if msg.get("type") == "reject":
            log.info("Call rejected by the other device.")
            return

        if msg.get("type") != "accept":
            raise RuntimeError(f"Unexpected message: {msg.get('type')}")

        log.info("Call accepted — establishing connection…")

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
            if pc.connectionState == "connected":
                asyncio.ensure_future(_play_ring(audio_cfg["sampleRate"], rings=1))
            elif pc.connectionState in ("failed", "closed"):
                await pc.close()

        try:
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

            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("type") != "answer":
                raise RuntimeError(f"Expected 'answer', got: {msg.get('type')}")

            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=msg["sdp"], type=msg["sdpType"])
            )
            log.info("Audio channel is live")
            await asyncio.Future()  # hold until connection drops
        finally:
            mic.stop()
            await pc.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, ConnectionClosed, WebSocketException):
        log.info("Call ended.")
