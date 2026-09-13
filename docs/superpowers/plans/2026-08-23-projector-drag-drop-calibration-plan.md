# Raspberry Pi Wall Projector Drag & Drop Calibration Plan

**Date:** 2026-08-23  
**Status:** Deployed & Live on `mira@mira.local` (`192.168.1.174`)

---

## 1. System Architecture

```
[ Raspberry Pi 4B (Debian Bookworm 64-bit Lite) ]
  ├── /dev/video0 (Generic USB UVC Camera, 1280x720 @ 30fps)
  ├── HDMI-A-1 (Projector, 1920x1080 @ 60Hz forced)
  ├── systemd: mira-calib.service (FastAPI + OpenCV + WebSockets on :5000)
  │     ├── /video_feed -> MJPEG Camera Stream with ArUco Overlays
  │     ├── /projector -> Fullscreen #000000 true-black canvas with ArUco markers & keystone warp
  │     ├── /control -> Interactive Mac GUI with draggable corner pins directly over camera feed
  │     └── /api/calibrate -> Computes 3x3 Homography Matrix H and saves to wall_calibration.json
  └── systemd: mira-kiosk.service (Wayland cage compositor + Chromium Kiosk on tty1)
```

---

## 2. Live URLs & Access

- **Mac Dashboard:** `http://mira.local:5000/control` (or `http://192.168.1.174:5000/control`)
- **Projector Output:** `http://localhost:5000/projector`
- **SSH Host:** `ssh mira@mira.local`

---

## 3. Calibration Workflow (When Resuming)

1. Open `http://mira.local:5000/control` in Chrome on your Mac.
2. Drag the 4 corner handles (TL, TR, BR, BL) on the live camera view until the projected box aligns squarely with your desired wall surface.
3. Or click **"🧲 Auto-Snap to Detected Markers"** to lock the target handles onto the detected ArUco centers.
4. Click **"⚡ SOLVE HOMOGRAPHY (H)"** to write the computed $3\times3$ homography matrix to `vision/wall_calibration.json`.
5. Once calibrated, the projection matrix transforms all future M.I.R.A. wall widgets, gesture cursors, and touch targets to align accurately on the angled wall.
