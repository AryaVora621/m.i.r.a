# Specification: Gesture Vocabulary & Spatial Projector Interaction

> **Date**: August 17, 2026  
> **Status**: Approved Design Specification  
> **Location**: `docs/superpowers/specs/2026-08-17-gesture-vocabulary-and-spatial-projection.md`

---

## 1. Executive Summary

Jarvis features physical spatial interaction in the user's room via a Raspberry Pi + Projector node. The projector displays an ambient UI angled across 1 to 2 room walls. A wide-angle webcam tracks hand landmarks in real time, mapping gestures directly onto the wall projection. Additionally, an overhead desk webcam node provides visual awareness ("Jarvis vision") over the user's workspace.

---

## 2. Hardware Topology & Camera Setup

```
                     ┌────────────────────────────────┐
                     │ Room Projector (Pi Node)       │
                     │ Projects Ambient UI onto 1-2   │
                     │ Room Walls                     │
                     └───────────────┬────────────────┘
                                     │
                     ┌───────────────┴────────────────┐
                     │ Wide-Angle Wall Webcam         │
                     │ Tracks Spatial Hand Gestures   │
                     └────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│ Workspace Desk Node (Future/Phase 3)                   │
│ - Overhead Top-Down Webcam ("Jarvis Sight")            │
│ - Inspects desk objects, documents, and physical tools │
└────────────────────────────────────────────────────────┘
```

---

## 3. Gesture Vocabulary & Action Map

| Gesture | Hand Pose | Trigger Condition | System Action |
| :--- | :--- | :--- | :--- |
| **`POINT`** | Index finger extended, others curled | Single finger tip tracked | Moves spatial cursor across wall projection |
| **`PINCH`** | Index tip & Thumb tip distance $< 25\text{px}$ | Distance threshold met | Primary Select / Click UI widget |
| **`PINCH_DRAG`** | Pinch held while moving | Distance maintained across frames | Moves projected widget / window |
| **`SWIPE_LEFT`** | Open hand fast movement $+X \to -X$ | Velocity $> 800\text{px/s}$ | Previous workspace view / slide |
| **`SWIPE_RIGHT`**| Open hand fast movement $-X \to +X$ | Velocity $> 800\text{px/s}$ | Next workspace view / slide |
| **`PALM_STOP`** | Open palm facing camera | 5 fingertips extended | Pause TTS voice / Cancel active execution |
| **`FIST_GRAB`** | All fingers closed into fist | Distance to palm center minimal | Dismiss active modal / overlay |

---

## 4. 4-Point Homography Perspective Calibration Matrix ($H$)

Because wall projectors are angled relative to webcams, raw camera coordinates $(x_{cam}, y_{cam})$ are mapped to wall projection coordinates $(x_{wall}, y_{wall})$ using a $3\times3$ **Homography Transformation Matrix ($H$)**:

$$\begin{bmatrix} x_{wall} \\ y_{wall} \\ 1 \end{bmatrix} \sim H \begin{bmatrix} x_{cam} \\ y_{cam} \\ 1 \end{bmatrix}$$

### 4.1 Wall Calibration Sequence
1. Web UI renders 4 glowing targets in the corners of the wall projection.
2. User touches or points at each corner target sequentially.
3. Python `vision` service collects point pairs and computes matrix $H$ via OpenCV `cv2.findHomography()`.
4. Matrix $H$ is persisted in `vision/wall_calibration.json`.
