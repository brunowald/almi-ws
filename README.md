# almi — Two-Device WebRTC Voice System

Full-duplex, peer-to-peer voice communication between two Raspberry Pi devices. Both devices auto-connect on startup with no user interaction required.

```
Pi A (caller) ──── WebRTC P2P audio ────> Pi B (answerer)
      ^                                        ^
      └── WebSocket signaling (setup only) ───┘
                  (signaling server)
```

Audio flows directly between devices (peer-to-peer). The signaling server is only used during the initial handshake.

---

## Project Structure

```
almi-ws/
├── config/
│   └── config.json          # Edit signalingUrl before deploying
├── signaling-server/
│   ├── server.js
│   └── package.json
├── device-client/
│   ├── client.py
│   └── requirements.txt
└── README.md
```

---

## Quick Start

### Step 1 — Configure

Edit `config/config.json` and set `signalingUrl` to the IP of the machine running the signaling server:

```json
{
  "signalingUrl": "ws://192.168.1.50:8765",
  ...
}
```

Copy the entire `almi-ws/` directory to both Raspberry Pis (and the signaling server host if it's a separate machine).

---

### Step 2 — Start the Signaling Server

The signaling server can run on any always-reachable host (a VPS, one of the Pis, or any machine on the network).

**Requires:** Node.js 18+

```bash
cd almi-ws/signaling-server
npm install
node server.js
```

Expected output:
```
Signaling server listening on port 8765
```

To run as a background service with automatic restart, use a process manager like `pm2`:
```bash
npm install -g pm2
pm2 start server.js --name almi-signaling
pm2 save && pm2 startup
```

---

### Step 3 — Prepare Each Raspberry Pi

#### System packages

```bash
sudo apt update && sudo apt install -y \
  python3 python3-pip python3-venv python3-dev \
  portaudio19-dev libportaudio2 \
  libavformat-dev libavcodec-dev libavdevice-dev \
  libavutil-dev libswscale-dev libswresample-dev libavfilter-dev \
  libsrtp2-dev libopus-dev libssl-dev libvpx-dev \
  python3-cffi libffi-dev pkg-config gcc
```

> **PyAV note:** On Pi 4/5 running 64-bit Pi OS Bookworm, `pip install aiortc` downloads prebuilt aarch64 wheels and skips compilation. The FFmpeg dev headers above are only needed if pip falls back to source compilation.

#### Python virtual environment

**Pi OS Bookworm (Debian 12)** blocks system-wide `pip install` by policy — a venv is required:

```bash
python3 -m venv ~/almi-venv
source ~/almi-venv/bin/activate
pip install -r ~/almi-ws/device-client/requirements.txt
```

**Pi OS Bullseye (Debian 11):** venv is strongly recommended. If Python 3.9 is the default, install 3.11 first:
```bash
sudo apt install python3.11 python3.11-venv
python3.11 -m venv ~/almi-venv
source ~/almi-venv/bin/activate
pip install -r ~/almi-ws/device-client/requirements.txt
```

#### Verify audio devices

```bash
python3 -c "import sounddevice as sd; print(sd.query_devices())"
```

Confirm the correct default input (microphone) and output (speaker/headset) are shown. If the wrong device is selected, override in your shell or systemd unit:
```bash
# Find your device index from the output above, then:
export SD_DEFAULT_DEVICE="2,4"   # input_index,output_index
```
Or set the device name in `config/config.json` by adding an `"inputDevice"` / `"outputDevice"` field and reading it in `client.py`.

---

### Step 4 — Run the Client

On **Pi A** (caller):
```bash
source ~/almi-venv/bin/activate
DEVICE_ROLE=caller python3 ~/almi-ws/device-client/client.py
```

On **Pi B** (answerer):
```bash
source ~/almi-venv/bin/activate
DEVICE_ROLE=answerer python3 ~/almi-ws/device-client/client.py
```

Start the answerer first (it waits for the offer), then the caller. Both roles will reconnect automatically if the connection drops.

Expected log (both sides):
```
Connecting to ws://192.168.1.50:8765…
Joined room 'almi-room-001' as caller
ICE gathering started (caller)…
Offer sent, waiting for answer…
Answer received and applied
WebRTC connection state: connected
Remote audio track received — starting playback
WebRTC negotiation complete — audio channel is live
```

---

### Step 5 — Auto-Start on Boot (systemd)

Create `/etc/systemd/system/almi-client.service` on each Pi:

```ini
[Unit]
Description=Almi WebRTC Voice Client
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=pi
Environment=DEVICE_ROLE=caller
ExecStart=/home/pi/almi-venv/bin/python3 /home/pi/almi-ws/device-client/client.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Change `DEVICE_ROLE=caller` to `DEVICE_ROLE=answerer` on the other Pi.

```bash
sudo systemctl daemon-reload
sudo systemctl enable almi-client
sudo systemctl start almi-client

# Follow live logs:
sudo journalctl -u almi-client -f
```

---

## Hardware Recommendations

| Model | Suitability |
|-------|-------------|
| Pi 5 | Excellent — ample CPU for Opus encode/decode |
| Pi 4 (2GB+) | Recommended — comfortable CPU headroom |
| Pi 3B+ | Adequate — audio-only load is manageable |
| Pi Zero 2W | Minimum — works, but leave CPU headroom |
| Pi Zero (v1) | Not recommended — single-core ARMv6 too slow |

Opus audio codec is **entirely software-based** on Raspberry Pi (no hardware acceleration). CPU usage is typically <10% on a Pi 4 at 48kHz mono.

---

## Echo Handling

Without acoustic separation, the microphone picks up the speaker output, causing the remote party to hear their own voice echoed back.

### Option 1: Use headphones (recommended)

The simplest and most effective solution. No software configuration needed.

### Option 2: PulseAudio echo cancellation

Requires PulseAudio (`sudo apt install pulseaudio`):

```bash
pactl load-module module-echo-cancel \
  aec_method=webrtc \
  source_name=noecho \
  source_master=alsa_input.default \
  sink_name=noecho_sink \
  sink_master=alsa_output.default
```

Then set the default device to `noecho` in `~/.config/pulse/default.pa` or via environment. Adds ~50ms latency. Survives only until PulseAudio restarts; add to `/etc/pulse/default.pa` for persistence.

### Option 3: speexdsp (Python)

Install `speexdsp` (wraps libspeexdsp AEC):
```bash
pip install speexdsp
```
Requires passing the played-back audio as a far-end reference to the canceller — needs code changes in `client.py`. See [speexdsp docs](https://github.com/xiongyihui/speexdsp-python) for integration.

### Limitations

All software AEC approaches are tuned for speech and can produce artefacts with non-speech audio or sudden loud sounds. For production use, a USB headset with built-in hardware AEC (e.g., Jabra Speak series) is the most reliable option.

---

## Networking

### LAN (same network)
Works out of the box. ICE uses local host candidates; STUN is not needed.

### Across NAT (different home networks)
The STUN servers in `config.json` (Google's public STUN) allow ICE to discover public IP/port pairs and punch through most NAT configurations.

### Symmetric NAT limitation
If **both** devices are behind symmetric NAT (common on some ISPs and corporate networks), STUN alone cannot establish a direct connection. In that case, a **TURN relay server** is required. Add TURN credentials to `stunServers` in `config.json`:

```json
{
  "urls": "turn:your-turn-server.example.com:3478",
  "username": "user",
  "credential": "password"
}
```

Self-hosted TURN options: [coturn](https://github.com/coturn/coturn) (open source).

---

## Configuration Reference

`config/config.json`:

| Field | Description |
|-------|-------------|
| `signalingUrl` | WebSocket URL of the signaling server |
| `roomId` | Both devices must use the same room ID |
| `stunServers` | Array of STUN/TURN server objects (WebRTC RTCIceServer format) |
| `audio.sampleRate` | Sample rate in Hz (48000 recommended for Opus) |
| `audio.channels` | 1 = mono (recommended), 2 = stereo |
| `audio.frameSize` | Samples per frame (960 = 20ms at 48kHz — matches Opus packet time) |
| `audio.latency` | sounddevice latency hint: `"low"`, `"high"`, or seconds as float |

---

## Troubleshooting

**`DEVICE_ROLE environment variable must be 'caller' or 'answerer'`**
Set the env var before running: `DEVICE_ROLE=caller python3 client.py`

**`No such file or directory: config.json`**
Run the client from its own directory, or ensure `almi-ws/config/config.json` exists.

**`PortAudio not found` / `sounddevice` import error**
Install `portaudio19-dev`: `sudo apt install portaudio19-dev`

**Connection stalls at "Waiting for offer…"**
The caller may not have started yet, or the signaling server is unreachable. Check `signalingUrl` in `config.json` and confirm the server is running.

**`WebRTC connection state: failed`**
ICE negotiation failed. Check that STUN servers are reachable (`nc -u stun.l.google.com 19302`) and that the two devices are not both behind symmetric NAT.

**Audio device not found / wrong device**
Run `python3 -c "import sounddevice as sd; print(sd.query_devices())"` to list devices. Use ALSA device selection or sounddevice device index to override.

**High latency or dropouts**
Try increasing `audio.frameSize` to `1920` (40ms) in `config.json`. Also ensure the Pi is not CPU-throttled: `vcgencmd measure_temp` and check cooling.
