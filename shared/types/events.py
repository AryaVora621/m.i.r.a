"""
M.I.R.A. Shared Typed Event Models (Pydantic V2)
Mapping directly to `shared/schemas/*.json` and `shared/types/index.ts`.
"""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class GestureType(str, Enum):
    PINCH = "PINCH"
    POINT = "POINT"
    SWIPE_LEFT = "SWIPE_LEFT"
    SWIPE_RIGHT = "SWIPE_RIGHT"
    PALM_STOP = "PALM_STOP"
    HOVER = "HOVER"
    PINCH_DRAG = "PINCH_DRAG"
    FIST_GRAB = "FIST_GRAB"


class Coordinates3D(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    z: float


class GestureEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    timestamp: float
    gesture_type: GestureType
    coordinates: Coordinates3D
    confidence: float = Field(ge=0.0, le=1.0)


class SpeechState(str, Enum):
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"


class SpeechEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    timestamp: float
    state: SpeechState
    raw_transcript: str
    cleaned_prompt: str
    is_final: bool


class LyricLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_ms: int = Field(ge=0)
    text: str


class MediaEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    timestamp: float
    track: str
    artist: str
    album: str
    duration_ms: int = Field(ge=0)
    position_ms: int = Field(ge=0)
    is_playing: bool
    lyrics: Optional[List[LyricLine]] = None


class NodeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str
    target_node: str
    action: str
    payload: Dict[str, Any]
    timeout_ms: int = Field(ge=0)


class TelemetryStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


class TelemetryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_percent: float = Field(ge=0.0, le=100.0)
    vram_used_gb: float = Field(ge=0.0)
    vram_total_gb: float = Field(ge=0.0)
    fps: float = Field(ge=0.0)
    temp_c: float


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    timestamp: float
    status: TelemetryStatus
    metrics: TelemetryMetrics


class WandEventType(str, Enum):
    BUTTON_DOWN = "button_down"
    BUTTON_UP = "button_up"
    PTT_AUDIO = "ptt_audio"
    INSPECT_SNAP = "inspect_snap"


class WandButton(str, Enum):
    SHUTTER = "shutter"
    PTT = "ptt"
    INSPECT = "inspect"
    CYCLE_HUD = "cycle_hud"
    MUTE = "mute"


class WandEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: WandEventType
    button: WandButton
    gpio: int
    timestamp_ms: int
    payload: Optional[Union[str, Dict[str, Any]]] = None


class CalibrationStage(str, Enum):
    INIT = "INIT"
    DETECTED = "DETECTED"
    COMPUTED = "COMPUTED"
    VERIFIED = "VERIFIED"


class Point2D(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class CalibrationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    timestamp: float
    stage: CalibrationStage
    corners: List[Point2D] = Field(min_length=4, max_length=4)
    homography_matrix: List[List[float]] = Field(min_length=3, max_length=3)
    reprojection_error_px: float = Field(ge=0.0)


class MiraEventEnvelope(BaseModel):
    topic: str
    event: str
    sender_node: str
    timestamp: float
    data: Any
