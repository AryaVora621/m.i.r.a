"""
M.I.R.A. Projector Alignment & Optical Auto-Homography Calibration Server
Provides:
  - /projector: Fullscreen #000000 true-black canvas with 4-corner ArUco markers & real-time CSS/SVG perspective warp
  - /control: Interactive Mac dashboard with LIVE CAMERA VIEW and DIRECT DRAG-AND-DROP corner target overlay
  - /video_feed: MJPEG stream from /dev/video0 with ArUco detection overlay
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
import uvicorn

app = FastAPI(title="MIRA Projector Calibration")

CALIBRATION_FILE = Path.home() / "mira" / "vision" / "wall_calibration.json"
CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)

# Default keystone / corner pin offsets (percentage 0.0 to 1.0)
current_config = {
    "proj_width": 1920,
    "proj_height": 1080,
    "show_markers": True,
    "show_grid": True,
    "show_crosshair": True,
    "corners": {
        "tl": {"x": 0.22, "y": 0.28},
        "tr": {"x": 0.78, "y": 0.28},
        "br": {"x": 0.78, "y": 0.80},
        "bl": {"x": 0.22, "y": 0.80}
    },
    "marker_size_px": 420,
    "marker_green": 180
}

latest_detected_markers = {}
latest_detector_frame = None
connected_clients = set()
camera = None


def _compute_aruco_bit_matrices():
    """Derive the real DICT_4X4_50 payload bits for marker IDs 0-3 straight from
    OpenCV's own dictionary, so the projected markers are guaranteed to match what
    detect_aruco_in_frame() below can actually decode. Do not hand-roll these bits."""
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    matrices = {}
    for marker_id in range(4):
        img = cv2.aruco.generateImageMarker(aruco_dict, marker_id, 6)
        bits = (img > 128).astype(int)
        payload = bits[1:5, 1:5]  # strip the 1-module black border
        matrices[marker_id] = payload.tolist()
    return matrices


ARUCO_BIT_MATRICES_JSON = json.dumps(_compute_aruco_bit_matrices())

def get_camera():
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(0)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return camera


def detect_aruco_in_frame(frame):
    global latest_detected_markers

    # Preserve luminance contrast between the projected cells and marker
    # background. Under purple ambient light, the green cells can become
    # neutral without clipping. A one-sided chroma key would collapse both
    # neutral cells and negative-chroma purple regions to zero.
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

    try:
        detector_params = cv2.aruco.DetectorParameters()
    except AttributeError:
        detector_params = cv2.aruco.DetectorParameters_create()

    # Markers are large but blurred by projector focus/throw distance, so cell
    # edges bleed into neighbors. Sample only each cell's center (skip more of
    # the margin) and tolerate more bit errors during ID matching.
    detector_params.adaptiveThreshWinSizeMin = 3
    detector_params.adaptiveThreshWinSizeMax = 35
    detector_params.adaptiveThreshWinSizeStep = 4
    detector_params.polygonalApproxAccuracyRate = 0.05
    detector_params.perspectiveRemoveIgnoredMarginPerCell = 0.1
    detector_params.errorCorrectionRate = 0.8

    try:
        detector = cv2.aruco.ArucoDetector(aruco_dict, detector_params)
        corners, ids, rejected = detector.detectMarkers(gray)
    except AttributeError:
        corners, ids, rejected = cv2.aruco.detectMarkers(gray, aruco_dict, parameters=detector_params)

    detected = {}
    annotated = frame.copy()

    if ids is not None and len(ids) > 0:
        cv2.aruco.drawDetectedMarkers(annotated, corners, ids)
        for i, marker_id in enumerate(ids.flatten()):
            c = corners[i][0]
            center_x = float(np.mean(c[:, 0]))
            center_y = float(np.mean(c[:, 1]))
            detected[int(marker_id)] = {
                "center": [center_x, center_y],
                "corners": c.tolist()
            }

        if all(k in detected for k in [0, 1, 2, 3]):
            poly_pts = np.array([
                detected[0]["center"],
                detected[1]["center"],
                detected[2]["center"],
                detected[3]["center"]
            ], np.int32).reshape((-1, 1, 2))
            cv2.polylines(annotated, [poly_pts], isClosed=True, color=(0, 255, 128), thickness=3)
            cv2.putText(annotated, "4/4 MARKERS LOCKED", (30, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 128), 2)
        else:
            cv2.putText(annotated, f"DETECTED {len(detected)}/4 MARKERS (IDs: {list(detected.keys())})",
                        (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
    else:
        cv2.putText(annotated, "SEARCHING FOR MARKERS...", (30, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)

    latest_detected_markers = detected
    return detected, annotated


def generate_video_stream():
    global latest_detector_frame
    cam = get_camera()
    while True:
        success, frame = cam.read()
        if not success:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            cv2.putText(frame, "CAMERA UNAVAILABLE (/dev/video0)", (100, 360),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        else:
            # Keep a lossless copy of the exact BGR frame passed to detection.
            # /video_feed re-encodes the annotated frame as JPEG, which can hide
            # whether clipping happened in the camera or in our debug stream.
            latest_detector_frame = frame.copy()
            _, frame = detect_aruco_in_frame(frame)

        ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(0.033)


@app.get("/video_feed")
def video_feed():
    return StreamingResponse(generate_video_stream(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/config")
def get_config():
    return current_config


@app.get("/api/detected_markers")
def get_detected_markers():
    return latest_detected_markers


@app.get("/api/detector_frame.png")
def get_detector_frame():
    if latest_detector_frame is None:
        return JSONResponse(status_code=503, content={"error": "No camera frame captured yet"})

    ok, buffer = cv2.imencode(".png", latest_detector_frame)
    if not ok:
        return JSONResponse(status_code=500, content={"error": "Failed to encode detector frame"})
    return Response(content=buffer.tobytes(), media_type="image/png")


@app.post("/api/config")
async def update_config(config: Dict[str, Any]):
    global current_config
    current_config.update(config)
    msg = json.dumps({"type": "config_updated", "config": current_config})
    for ws in list(connected_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            connected_clients.discard(ws)
    return {"status": "ok", "config": current_config}


@app.post("/api/calibrate")
def run_calibration():
    cam = get_camera()
    success, frame = cam.read()
    if not success:
        return JSONResponse(status_code=500, content={"error": "Failed to capture camera frame"})

    detected, _ = detect_aruco_in_frame(frame)
    required_ids = [0, 1, 2, 3]
    if not all(k in detected for k in required_ids):
        missing = [k for k in required_ids if k not in detected]
        return JSONResponse(
            status_code=400,
            content={
                "error": f"Missing ArUco marker IDs: {missing}. Detected IDs: {list(detected.keys())}",
                "detected_count": len(detected)
            }
        )

    pts_cam = np.array([
        detected[0]["center"],
        detected[1]["center"],
        detected[2]["center"],
        detected[3]["center"]
    ], dtype=np.float32)

    pw = current_config["proj_width"]
    ph = current_config["proj_height"]
    c = current_config["corners"]
    pts_proj = np.array([
        [c["tl"]["x"] * pw, c["tl"]["y"] * ph],
        [c["tr"]["x"] * pw, c["tr"]["y"] * ph],
        [c["br"]["x"] * pw, c["br"]["y"] * ph],
        [c["bl"]["x"] * pw, c["bl"]["y"] * ph]
    ], dtype=np.float32)

    H, status = cv2.findHomography(pts_cam, pts_proj, cv2.RANSAC, 3.0)
    if H is None:
        return JSONResponse(status_code=500, content={"error": "Failed to compute Homography matrix"})

    pts_cam_homo = np.concatenate([pts_cam, np.ones((4, 1))], axis=1)
    reprojected = (H @ pts_cam_homo.T).T
    reprojected = reprojected[:, :2] / reprojected[:, 2:3]
    errors = np.linalg.norm(pts_proj - reprojected, axis=1)
    mean_error = float(np.mean(errors))

    calib_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reprojection_error": round(mean_error, 3),
        "matrix_h": H.tolist(),
        "resolution_proj": [pw, ph],
        "resolution_cam": [frame.shape[1], frame.shape[0]],
        "source_corners_cam": pts_cam.tolist(),
        "target_corners_proj": pts_proj.tolist()
    }

    with open(CALIBRATION_FILE, "w") as f:
        json.dump(calib_data, f, indent=2)

    return {
        "status": "success",
        "reprojection_error_px": round(mean_error, 3),
        "calibration": calib_data
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "update_corners":
                current_config["corners"] = msg["corners"]
                for client in list(connected_clients):
                    if client != websocket:
                        try:
                            await client.send_text(json.dumps({"type": "config_updated", "config": current_config}))
                        except Exception:
                            connected_clients.discard(client)
    except WebSocketDisconnect:
        connected_clients.discard(websocket)


@app.get("/projector", response_class=HTMLResponse)
def projector_view():
    return _PROJECTOR_HTML.replace("__ARUCO_BIT_MATRICES_JSON__", ARUCO_BIT_MATRICES_JSON)


_PROJECTOR_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>MIRA Projector Calibration Display</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; }
    html, body {
      width: 100vw; height: 100vh;
      background-color: #2b2b2b;
      color: #ffffff;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
    }
    #stage {
      position: absolute;
      top: 0; left: 0; width: 100%; height: 100%;
      background: #2b2b2b;
    }
    svg {
      position: absolute;
      top: 0; left: 0; width: 100%; height: 100%;
    }
    .grid-line {
      stroke: rgba(0, 255, 204, 0.2);
      stroke-width: 1;
      stroke-dasharray: 6, 6;
    }
    .boundary-box {
      fill: rgba(0, 255, 204, 0.03);
      stroke: #00ffcc;
      stroke-width: 3;
      filter: drop-shadow(0 0 10px rgba(0, 255, 204, 0.7));
    }
    .crosshair {
      stroke: #ff9900;
      stroke-width: 2;
    }
    .focus-ring {
      fill: none;
      stroke: rgba(255, 255, 255, 0.35);
      stroke-width: 1.5;
    }
    .marker-box {
      position: absolute;
      background: #000000;
      padding: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.05s ease-out;
    }
  </style>
</head>
<body>
  <div id="stage">
    <svg id="grid-svg"></svg>
    <div id="markers-container"></div>
  </div>

  <script>
    const ARUCO_4X4 = __ARUCO_BIT_MATRICES_JSON__;

    function renderArucoSVG(id, size) {
      const matrix = ARUCO_4X4[id] || ARUCO_4X4[0];
      const cellSize = size / 6;
      const green = Math.max(0, Math.min(255, Number(config.marker_green ?? 180)));
      const markerColor = `rgb(0, ${green}, 0)`;
      let rects = `<rect width="${size}" height="${size}" fill="#000000"/>`;
      for (let r = 0; r < 4; r++) {
        for (let c = 0; c < 4; c++) {
          if (matrix[r][c] === 1) {
            const x = (c + 1) * cellSize;
            const y = (r + 1) * cellSize;
            rects += `<rect x="${x}" y="${y}" width="${cellSize}" height="${cellSize}" fill="${markerColor}"/>`;
          }
        }
      }
      return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${rects}</svg>`;
    }

    let config = {};

    function updateView() {
      const W = window.innerWidth;
      const H = window.innerHeight;
      const svg = document.getElementById('grid-svg');
      const mc = document.getElementById('markers-container');
      
      const c = config.corners || {
        tl: {x: 0.05, y: 0.05},
        tr: {x: 0.95, y: 0.05},
        br: {x: 0.95, y: 0.95},
        bl: {x: 0.05, y: 0.95}
      };
      
      const mSize = config.marker_size_px || 140;

      let svgHtml = '';
      const pts = `${c.tl.x * W},${c.tl.y * H} ${c.tr.x * W},${c.tr.y * H} ${c.br.x * W},${c.br.y * H} ${c.bl.x * W},${c.bl.y * H}`;
      svgHtml += `<polygon points="${pts}" class="boundary-box"/>`;

      // Diagonal alignment cross
      svgHtml += `<line x1="${c.tl.x * W}" y1="${c.tl.y * H}" x2="${c.br.x * W}" y2="${c.br.y * H}" stroke="rgba(0,255,204,0.2)" stroke-width="1.5" stroke-dasharray="4,4"/>`;
      svgHtml += `<line x1="${c.tr.x * W}" y1="${c.tr.y * H}" x2="${c.bl.x * W}" y2="${c.bl.y * H}" stroke="rgba(0,255,204,0.2)" stroke-width="1.5" stroke-dasharray="4,4"/>`;

      // Center crosshair & focus rings
      const cx = W / 2, cy = H / 2;
      svgHtml += `
        <line x1="${cx - 80}" y1="${cy}" x2="${cx + 80}" y2="${cy}" class="crosshair"/>
        <line x1="${cx}" y1="${cy - 80}" x2="${cx}" y2="${cy + 80}" class="crosshair"/>
        <circle cx="${cx}" cy="${cy}" r="40" class="focus-ring"/>
        <circle cx="${cx}" cy="${cy}" r="90" class="focus-ring"/>
        <circle cx="${cx}" cy="${cy}" r="160" class="focus-ring"/>
      `;

      svg.innerHTML = svgHtml;

      const markers = [
        { id: 0, x: c.tl.x * W - mSize/2, y: c.tl.y * H - mSize/2 },
        { id: 1, x: c.tr.x * W - mSize/2, y: c.tr.y * H - mSize/2 },
        { id: 2, x: c.br.x * W - mSize/2, y: c.br.y * H - mSize/2 },
        { id: 3, x: c.bl.x * W - mSize/2, y: c.bl.y * H - mSize/2 }
      ];

      let markersHtml = '';
      markers.forEach(m => {
        markersHtml += `
          <div class="marker-box" style="left: ${m.x}px; top: ${m.y}px; width: ${mSize}px; height: ${mSize}px;">
            ${renderArucoSVG(m.id, mSize - 12)}
          </div>
        `;
      });
      mc.innerHTML = markersHtml;
    }

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'config_updated') {
        config = data.config;
        updateView();
      }
    };

    fetch('/api/config')
      .then(r => r.json())
      .then(cfg => {
        config = cfg;
        updateView();
      });

    window.addEventListener('resize', updateView);
  </script>
</body>
</html>
    """


@app.get("/control", response_class=HTMLResponse)
def control_view():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>MIRA Drag & Drop Calibration Overlay</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; }
    body {
      background: #0b0e14;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
      padding: 20px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid #1e293b;
    }
    .title { font-size: 18px; font-weight: 700; color: #00ffcc; }
    .badge {
      background: #10b98122;
      border: 1px solid #10b981;
      color: #10b981;
      padding: 4px 12px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
    }
    .layout {
      display: grid;
      grid-template-columns: 1fr 340px;
      gap: 20px;
    }
    .viewport-card {
      background: #121722;
      border: 1px solid #1e293b;
      border-radius: 12px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .canvas-container {
      position: relative;
      width: 100%;
      max-width: 960px;
      aspect-ratio: 16 / 9;
      background: #000000;
      border-radius: 8px;
      overflow: hidden;
      border: 2px solid #334155;
      cursor: crosshair;
    }
    #cam-img {
      position: absolute;
      top: 0; left: 0;
      width: 100%; height: 100%;
      object-fit: fill;
      pointer-events: none;
    }
    #overlay-canvas {
      position: absolute;
      top: 0; left: 0;
      width: 100%; height: 100%;
      z-index: 10;
    }
    .sidebar {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .card {
      background: #121722;
      border: 1px solid #1e293b;
      border-radius: 12px;
      padding: 16px;
    }
    .card h3 {
      font-size: 13px;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
    }
    .btn {
      width: 100%;
      padding: 12px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      border: none;
      transition: all 0.15s ease;
      margin-bottom: 8px;
    }
    .btn-primary {
      background: #00ffcc;
      color: #050505;
    }
    .btn-primary:hover { background: #33ffd6; }
    .btn-secondary {
      background: #1e293b;
      color: #cbd5e1;
      border: 1px solid #334155;
    }
    .btn-secondary:hover { background: #334155; color: #ffffff; }
    .btn-preset {
      background: #0f172a;
      color: #38bdf8;
      border: 1px solid #1e293b;
      padding: 8px;
      font-size: 12px;
    }
    .btn-preset:hover { background: #1e293b; }
    .coords-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      font-size: 11px;
      font-family: monospace;
      color: #cbd5e1;
      margin-bottom: 12px;
    }
    .coord-item {
      background: #090c10;
      padding: 6px 8px;
      border-radius: 4px;
      border-left: 3px solid #00ffcc;
    }
    .coord-item.tl { border-color: #00ffcc; }
    .coord-item.tr { border-color: #ffaa00; }
    .coord-item.br { border-color: #10b981; }
    .coord-item.bl { border-color: #a855f7; }
    .status-log {
      padding: 12px;
      background: #090c10;
      border-radius: 6px;
      font-size: 11px;
      font-family: monospace;
      color: #38bdf8;
      white-space: pre-wrap;
      max-height: 140px;
      overflow-y: auto;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="title">🖐️ M.I.R.A. Interactive Wall Overlay & Optical Calibrator</div>
    <div class="badge">Connected (192.168.1.174)</div>
  </div>

  <div class="layout">
    <div class="viewport-card">
      <div style="width: 100%; display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 12px; color: #94a3b8;">
        <span>🎯 DRAG THE 4 CORNERS TO MATCH YOUR PROJECTOR WALL TARGET</span>
        <span>LIVE FEED: 30 FPS</span>
      </div>

      <div class="canvas-container" id="container">
        <img id="cam-img" src="/video_feed" alt="Camera Feed">
        <canvas id="overlay-canvas"></canvas>
      </div>

      <div style="margin-top: 10px; font-size: 12px; color: #64748b; text-align: center;">
        💡 Click and drag any of the 4 colored glowing handles. The projector updates instantly on your wall.
      </div>
    </div>

    <div class="sidebar">
      <div class="card">
        <h3>Corner Target Coordinates</h3>
        <div class="coords-grid">
          <div class="coord-item tl" id="txt-tl">TL: 5.0%, 5.0%</div>
          <div class="coord-item tr" id="txt-tr">TR: 95.0%, 5.0%</div>
          <div class="coord-item bl" id="txt-bl">BL: 5.0%, 95.0%</div>
          <div class="coord-item br" id="txt-br">BR: 95.0%, 95.0%</div>
        </div>

        <button class="btn btn-primary" onclick="solveCalibration()">⚡ SOLVE HOMOGRAPHY (H)</button>
        <button class="btn btn-secondary" onclick="autoSnapToMarkers()">🧲 Auto-Snap to Detected Markers</button>
      </div>

      <div class="card">
        <h3>Quick Presets</h3>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px;">
          <button class="btn btn-preset" onclick="setPreset(0.05)">Default (5%)</button>
          <button class="btn btn-preset" onclick="setPreset(0.10)">Inset (10%)</button>
          <button class="btn btn-preset" onclick="setPreset(0.15)">Inset (15%)</button>
          <button class="btn btn-preset" onclick="setPreset(0.00)">Full (0%)</button>
        </div>
      </div>

      <div class="card">
        <h3>Calibration Output</h3>
        <div class="status-log" id="log-box">Drag corners to align on wall, then click Solve Homography.</div>
      </div>
    </div>
  </div>

  <script>
    const canvas = document.getElementById('overlay-canvas');
    const ctx = canvas.getContext('2d');
    const container = document.getElementById('container');

    let config = {
      corners: {
        tl: {x: 0.05, y: 0.05},
        tr: {x: 0.95, y: 0.05},
        br: {x: 0.95, y: 0.95},
        bl: {x: 0.05, y: 0.95}
      }
    };

    const HANDLE_COLORS = {
      tl: '#00ffcc', // Cyan
      tr: '#ffaa00', // Amber
      br: '#10b981', // Emerald
      bl: '#a855f7'  // Purple
    };

    let draggingKey = null;

    function resizeCanvas() {
      canvas.width = container.clientWidth;
      canvas.height = container.clientHeight;
      draw();
    }

    window.addEventListener('resize', resizeCanvas);

    function draw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const W = canvas.width;
      const H = canvas.height;
      const c = config.corners;

      const pts = {
        tl: { x: c.tl.x * W, y: c.tl.y * H },
        tr: { x: c.tr.x * W, y: c.tr.y * H },
        br: { x: c.br.x * W, y: c.br.y * H },
        bl: { x: c.bl.x * W, y: c.bl.y * H }
      };

      // Draw bounding quad
      ctx.beginPath();
      ctx.moveTo(pts.tl.x, pts.tl.y);
      ctx.lineTo(pts.tr.x, pts.tr.y);
      ctx.lineTo(pts.br.x, pts.br.y);
      ctx.lineTo(pts.bl.x, pts.bl.y);
      ctx.closePath();
      ctx.fillStyle = 'rgba(0, 255, 204, 0.08)';
      ctx.fill();
      ctx.strokeStyle = '#00ffcc';
      ctx.lineWidth = 2.5;
      ctx.stroke();

      // Draw diagonals
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
      ctx.moveTo(pts.tl.x, pts.tl.y);
      ctx.lineTo(pts.br.x, pts.br.y);
      ctx.moveTo(pts.tr.x, pts.tr.y);
      ctx.lineTo(pts.bl.x, pts.bl.y);
      ctx.stroke();
      ctx.setLineDash([]);

      // Draw Draggable Handles
      Object.keys(pts).forEach(key => {
        const p = pts[key];
        const color = HANDLE_COLORS[key];
        const isHover = (draggingKey === key);

        // Glow ring
        ctx.beginPath();
        ctx.arc(p.x, p.y, isHover ? 18 : 14, 0, Math.PI * 2);
        ctx.fillStyle = color + '44';
        ctx.fill();

        // Outer border
        ctx.beginPath();
        ctx.arc(p.x, p.y, isHover ? 12 : 9, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();

        // Label
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 11px monospace';
        ctx.fillText(key.toUpperCase(), p.x + 14, p.y + 4);
      });

      // Update text boxes
      document.getElementById('txt-tl').innerText = `TL: ${(c.tl.x*100).toFixed(1)}%, ${(c.tl.y*100).toFixed(1)}%`;
      document.getElementById('txt-tr').innerText = `TR: ${(c.tr.x*100).toFixed(1)}%, ${(c.tr.y*100).toFixed(1)}%`;
      document.getElementById('txt-br').innerText = `BR: ${(c.br.x*100).toFixed(1)}%, ${(c.br.y*100).toFixed(1)}%`;
      document.getElementById('txt-bl').innerText = `BL: ${(c.bl.x*100).toFixed(1)}%, ${(c.bl.y*100).toFixed(1)}%`;
    }

    function getMousePos(e) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: (e.clientX - rect.left) / rect.width,
        y: (e.clientY - rect.top) / rect.height
      };
    }

    canvas.addEventListener('mousedown', (e) => {
      const pos = getMousePos(e);
      const W = canvas.width, H = canvas.height;
      const clickRadiusPx = 25;

      let found = null;
      Object.keys(config.corners).forEach(key => {
        const c = config.corners[key];
        const dx = (pos.x - c.x) * W;
        const dy = (pos.y - c.y) * H;
        if (Math.hypot(dx, dy) < clickRadiusPx) {
          found = key;
        }
      });

      if (found) {
        draggingKey = found;
        draw();
      }
    });

    window.addEventListener('mousemove', (e) => {
      if (!draggingKey) return;
      const pos = getMousePos(e);
      config.corners[draggingKey] = {
        x: Math.max(0, Math.min(1, pos.x)),
        y: Math.max(0, Math.min(1, pos.y))
      };
      draw();
      sendCornerUpdate();
    });

    window.addEventListener('mouseup', () => {
      if (draggingKey) {
        draggingKey = null;
        draw();
      }
    });

    // WebSocket communication
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws`);

    function sendCornerUpdate() {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'update_corners',
          corners: config.corners
        }));
      }
    }

    function setPreset(inset) {
      config.corners = {
        tl: {x: inset, y: inset},
        tr: {x: 1 - inset, y: inset},
        br: {x: 1 - inset, y: 1 - inset},
        bl: {x: inset, y: 1 - inset}
      };
      draw();
      sendCornerUpdate();
      fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(config)
      });
    }

    async function autoSnapToMarkers() {
      const log = document.getElementById('log-box');
      log.innerText = "Querying live ArUco detection...";
      try {
        const res = await fetch('/api/detected_markers');
        const detected = await res.json();
        if (detected['0'] && detected['1'] && detected['2'] && detected['3']) {
          // Normalize cam coords (1280x720) to 0.0 - 1.0
          config.corners.tl = { x: detected['0'].center[0] / 1280, y: detected['0'].center[1] / 720 };
          config.corners.tr = { x: detected['1'].center[0] / 1280, y: detected['1'].center[1] / 720 };
          config.corners.br = { x: detected['2'].center[0] / 1280, y: detected['2'].center[1] / 720 };
          config.corners.bl = { x: detected['3'].center[0] / 1280, y: detected['3'].center[1] / 720 };
          draw();
          sendCornerUpdate();
          log.innerText = "🧲 Successfully snapped target overlay to all 4 detected markers!";
        } else {
          log.innerText = `Detected ${Object.keys(detected).length}/4 markers. Make sure all 4 are visible.`;
        }
      } catch (err) {
        log.innerText = `Auto-snap failed: ${err.message}`;
      }
    }

    async function solveCalibration() {
      const log = document.getElementById('log-box');
      log.innerText = "⚡ Capturing camera frame & calculating 3x3 Homography Matrix H...";
      try {
        const res = await fetch('/api/calibrate', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          log.innerText = `✅ CALIBRATION SUCCESS!\nMean Error: ${data.reprojection_error_px} px\nSaved to: vision/wall_calibration.json\n\nMatrix H:\n${JSON.stringify(data.calibration.matrix_h, null, 2)}`;
        } else {
          log.innerText = `❌ Error: ${data.error || JSON.stringify(data)}`;
        }
      } catch (err) {
        log.innerText = `❌ Request failed: ${err.message}`;
      }
    }

    fetch('/api/config')
      .then(r => r.json())
      .then(cfg => {
        config = cfg;
        resizeCanvas();
      });
  </script>
</body>
</html>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
