# Raspberry Pi Projector & Vision Node Bringup Plan

**Date:** 2026-08-21  
**Goal:** Deploy M.I.R.A. head node to a ceiling-mounted Raspberry Pi connected via HDMI to the wall projector, using a generic USB camera and an ultra-lightweight Wayland display module (`cage`) on Raspberry Pi OS Lite (64-bit).

---

## 1. Hardware Configuration

| Component | Specification | Connection | Role |
| :--- | :--- | :--- | :--- |
| **Compute** | Raspberry Pi 4B / 5 (ARM64) | Ceiling Mounted | Room node host, projector display, mic/speaker client |
| **OS** | Raspberry Pi OS Lite (64-bit Bookworm) | MicroSD / NVMe | Minimal ~150MB footprint, zero desktop clutter |
| **Display** | Wall Projector | Micro-HDMI / HDMI | Fullscreen `#000000` ambient UI |
| **Vision** | Generic USB Camera (UVC) | USB 3.0 Port | OpenCV hand/presence tracking (`/dev/video0`) |
| **Audio** | USB Mic / Speaker or I2S | USB / 3.5mm / Bluetooth | Voice loop audio capture and playback |

---

## 2. Architecture & Service Stack

```
[ Raspberry Pi OS Lite (64-bit) ]
  ├── systemd: mira-kiosk.service
  │     └── cage (Wayland Kiosk Compositor, ~5MB)
  │           └── Chromium / WebKit Kiosk (`--kiosk --incognito http://localhost:3000`)
  ├── systemd: mira-daemon.service (Rust WebSocket Gateway + Registry)
  ├── systemd: mira-guardrail.service (Rust Policy Engine + Dev Signer)
  ├── systemd: mira-voice.service (Python 3.12 Voice Loop + Sherpa-ONNX + Silero VAD)
  └── systemd: mira-vision.service (Python OpenCV /dev/video0 + MediaPipe Tracking)
```

---

## 3. Implementation Steps When Resuming

### Step 1: Flash & Initial Boot
1. Flash **Raspberry Pi OS Lite (64-bit)** using Raspberry Pi Imager.
2. Configure settings (gear icon):
   - Hostname: `mira`
   - User credentials & Wi-Fi details.
   - Enable SSH.
3. Power on Pi connected to Projector + USB Camera.
4. SSH into the Pi: `ssh <user>@mira.local`.

### Step 2: System Packages & Toolchains
Install core build tools, Python 3.12, audio/video drivers, and the minimal Wayland kiosk:
```bash
sudo apt update && sudo apt install -y \
  git curl build-essential \
  pkg-config libssl-dev \
  portaudio19-dev libasound2-dev libsndfile1-dev \
  v4l-utils cage chromium-browser \
  python3-pip python3-venv
```

### Step 3: Clone & Build Monorepo
```bash
git clone https://github.com/AryaVora621/m.i.r.a.git ~/mira
cd ~/mira
git checkout feat/mira-voice-loop

# Build Rust Daemon & Guardrail
cargo build --release

# Setup Python Voice Environment
python3 -m venv voice/.venv
source voice/.venv/bin/activate
pip install -r voice/requirements.txt
```

### Step 4: Configure Projector Kiosk (`systemd`)
Create `/etc/systemd/system/mira-kiosk.service`:
```ini
[Unit]
Description=MIRA Projector Ambient Kiosk
After=network.target

[Service]
User=aryavora
Environment=WLR_LIBINPUT_NO_DEVICES=1
ExecStart=/usr/bin/cage -- /usr/bin/chromium-browser \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --check-for-update-interval=31536000 \
  --app=http://localhost:3000
Restart=always

[Install]
WantedBy=graphical.target
```

### Step 5: Verify Camera & Audio
```bash
# Verify USB Camera video node
v4l2-ctl --list-devices

# Verify audio devices
arecord -l && aplay -l
```
