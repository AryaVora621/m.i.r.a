# Last Checkpoint

**Date:** 2026-08-23
**Agent:** `/root`
**Mode:** Debugger, ArUco calibration under standing purple LED ambient light

## Completed

- Confirmed the camera feeds OpenCV uncompressed `YUYV 4:2:2` at 1280x720. The browser `/video_feed` adds a later JPEG encode, but that encode is not in the detection path.
- Added `/api/detector_frame.png`, a lossless snapshot of the exact BGR frame passed to ArUco detection.
- Added `marker_green` to the live projector configuration so brightness can be tuned without another deployment. It remains at 180 because dimming was not needed.
- Corrected the prior clipping diagnosis with lossless pixel samples. Green cells measured about BGR `[212, 211, 213]`, so they were neutral but not clipped. Purple marker backgrounds retained signed chroma around `-37` to `-53`.
- Found the actual fourth bug: the one-sided chroma conversion clipped all negative values to zero, collapsing neutral cells and purple backgrounds together before ArUco saw them.
- Restored grayscale luminance preprocessing and tuned ArUco sampling to `adaptiveThreshWinSizeMin=3`, `adaptiveThreshWinSizeMax=35`, `adaptiveThreshWinSizeStep=4`, and `perspectiveRemoveIgnoredMarginPerCell=0.1`.
- Verified the fix against the saved lossless purple-light frame: 4/4 IDs detected.
- Deployed the fix to `/home/mira/calibration_server.py` and restarted both Pi services.
- Verified live purple-light detection: 4/4 markers in 6 of 7 populated samples. The remaining sample detected 3/4.
- Ran `POST /api/calibrate`. It succeeded on the first attempt and persisted `/home/mira/mira/vision/wall_calibration.json` with all four marker centers and a valid 3x3 matrix.

## Current State

- `mira-calib.service`: active
- `mira-kiosk.service`: active
- Purple LED ambient mode: supported by automatic marker detection
- Marker color: green level 180
- Current projector corners: `tl=(0.22,0.28)`, `tr=(0.78,0.28)`, `br=(0.78,0.80)`, `bl=(0.22,0.80)`
- Saved calibration timestamp: `2026-08-24T02:48:24Z`
- Saved reprojection error: `0.0 px` for the four exact point correspondences
- User confirmed the projected wall alignment is calibrated.
- Pi deployment preflight: ARM64 Raspberry Pi OS, 106 GB free storage, 3 GB available RAM, Python 3.13.5, USB camera microphone, and HDMI/headphone playback are available.
- Git, Cargo, and Rust are not installed on the Pi.
- The current voice dependency set requires Python 3.12, while the Pi has Python 3.13.5.
- The guardrail's implemented volume handler uses macOS `osascript`, so Pi volume control needs a Linux backend before it can provide the same real capability.

## Next Action

Write and execute a Pi runtime deployment subplan. Deploy the Rust daemon and guardrail as managed services first, add a Linux audio capability backend, then split the voice loop so the Pi keeps only latency-sensitive room-node work and heavier STT/TTS can move to the homelab worker.

## Human Decisions Needed

None for the deployment preflight.
