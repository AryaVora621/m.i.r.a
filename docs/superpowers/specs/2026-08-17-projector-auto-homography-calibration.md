# Specification: Projector Optical Auto-Homography Calibration Loop

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-projector-auto-homography-calibration.md`

---

## 1. Executive Summary

This specification defines the zero-friction, automated 4-point homography matrix ($H$) optical calibration system for the Raspberry Pi + Projector node. Rather than requiring manual 4-corner touching, the projector flashes a high-contrast 4-corner **ArUco Fiducial Marker Pattern** for 1.5 seconds upon startup or node movement. The wall webcam captures the projection, OpenCV auto-detects the 4 marker corners, and computes the $3\times3$ homography matrix $H$ with sub-pixel accuracy.

---

## 2. Calibration Sequence & Timing

```
[ Raspberry Pi Boot / Displacement Detected ]
                      │
                      ▼
┌──────────────────────────────────────────┐
│ 1. Web UI Flashes ArUco Pattern (1.5s)   │ ArUco DICT_4X4_50 in 4 corners
└─────────────────────┬────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────┐
│ 2. Wall Webcam Captures Calibration Frame│ High-gain frame capture via OpenCV
└─────────────────────┬────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────┐
│ 3. `cv2.aruco.detectMarkers()` Executed │ Extracts (x_cam, y_cam) per marker ID
└─────────────────────┬────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────┐
│ 4. `cv2.findHomography()` Computes H     │ Solves 3x3 Homography Matrix H
└─────────────────────┬────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────┐
│ 5. Save Matrix H & Transition to UI      │ Persisted to `wall_calibration.json`
└──────────────────────────────────────────┘
```

---

## 3. Mathematical & OpenCV Technical Specification

### 3.1 ArUco Pattern Layout
- **Dictionary**: `cv2.aruco.DICT_4X4_50` (low computational overhead, high contrast).
- **Corner Assignments**:
  - `ID 0`: Top-Left corner $(0.05 \times W_{proj}, 0.05 \times H_{proj})$
  - `ID 1`: Top-Right corner $(0.95 \times W_{proj}, 0.05 \times H_{proj})$
  - `ID 2`: Bottom-Right corner $(0.95 \times W_{proj}, 0.95 \times H_{proj})$
  - `ID 3`: Bottom-Left corner $(0.05 \times W_{proj}, 0.95 \times H_{proj})$

### 3.2 Matrix Calculation
Given project destination coordinates $\mathbf{X}_{proj} = \{(x_i, y_i)\}_{i=0}^3$ and camera source points $\mathbf{X}_{cam} = \{(u_i, v_i)\}_{i=0}^3$:

$$\begin{bmatrix} x_i \\ y_i \\ 1 \end{bmatrix} \sim H \begin{bmatrix} u_i \\ v_i \\ 1 \end{bmatrix}$$

- **Solver**: OpenCV `cv2.findHomography(pts_cam, pts_proj, cv2.RANSAC, 3.0)`.
- **Validation**: Reprojection error must be $< 2.5\text{px}$. If error exceeds threshold (e.g. shadow or obstruction), auto-retry flash once before notifying UI.

---

## 4. Auto-Triggering Conditions

The optical calibration loop executes automatically under any of the following triggers:
1. **Node Cold Boot**: System startup on Raspberry Pi.
2. **Physical Displacement Detection**: MPU6050 / Accelerometer on Pi detects node movement $\Delta \theta > 2^\circ$.
3. **Voice / Web UI Command**: Voice command `"Jarvis calibrate projector"` or UI button click.

---

## 5. Storage Schema (`vision/wall_calibration.json`)

```json
{
  "timestamp": "2026-08-17T11:37:00Z",
  "reprojection_error": 0.84,
  "matrix_h": [
    [1.2451, -0.0123, -45.2],
    [0.0054, 1.1892, -32.1],
    [0.00001, -0.00003, 1.0]
  ],
  "resolution_proj": [1920, 1080],
  "resolution_cam": [1280, 720]
}
```

---

## 6. Implementation Plan
1. Create `vision/auto_calibrate.py` module leveraging `opencv-contrib-python`.
2. Add `/calibrate` websocket endpoint to TypeScript ambient web dashboard.
3. Test ArUco detection across ambient room lighting conditions (daylight vs dark).
