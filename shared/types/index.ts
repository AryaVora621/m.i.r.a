/**
 * M.I.R.A. Shared TypeScript Type Definitions
 * 
 * Auto-compatible with JSON Schemas under `shared/schemas/`.
 * Governs IPC, WebSocket RPC, and cross-node microservice communications.
 */

export type GestureType =
  | 'PINCH'
  | 'POINT'
  | 'SWIPE_LEFT'
  | 'SWIPE_RIGHT'
  | 'PALM_STOP'
  | 'HOVER'
  | 'PINCH_DRAG'
  | 'FIST_GRAB';

export interface Coordinates3D {
  x: number;
  y: number;
  z: number;
}

export interface GestureEvent {
  node_id: string;
  timestamp: number;
  gesture_type: GestureType;
  coordinates: Coordinates3D;
  confidence: number;
}

export type SpeechState = 'LISTENING' | 'THINKING' | 'SPEAKING';

export interface SpeechEvent {
  node_id: string;
  timestamp: number;
  state: SpeechState;
  raw_transcript: string;
  cleaned_prompt: string;
  is_final: boolean;
}

export interface LyricLine {
  time_ms: number;
  text: string;
}

export interface MediaEvent {
  node_id: string;
  timestamp: number;
  track: string;
  artist: string;
  album: string;
  duration_ms: number;
  position_ms: number;
  is_playing: boolean;
  lyrics?: LyricLine[];
}

export interface NodeCommand<T = Record<string, unknown>> {
  command_id: string;
  target_node: string;
  action: string;
  payload: T;
  timeout_ms: number;
}

export type TelemetryStatus = 'OK' | 'DEGRADED' | 'ERROR';

export interface TelemetryMetrics {
  cpu_percent: number;
  vram_used_gb: number;
  vram_total_gb: number;
  fps: number;
  temp_c: number;
}

export interface TelemetryEvent {
  node_id: string;
  timestamp: number;
  status: TelemetryStatus;
  metrics: TelemetryMetrics;
}

export type WandEventType = 'button_down' | 'button_up' | 'ptt_audio' | 'inspect_snap';
export type WandButton = 'shutter' | 'ptt' | 'inspect' | 'cycle_hud' | 'mute';

export interface WandEvent {
  event: WandEventType;
  button: WandButton;
  gpio: number;
  timestamp_ms: number;
  payload?: string | Record<string, unknown> | null;
}

export type CalibrationStage = 'INIT' | 'DETECTED' | 'COMPUTED' | 'VERIFIED';

export interface Point2D {
  x: number;
  y: number;
}

export interface CalibrationEvent {
  node_id: string;
  timestamp: number;
  stage: CalibrationStage;
  corners: [Point2D, Point2D, Point2D, Point2D] | Point2D[];
  homography_matrix: number[][]; // 3x3 matrix
  reprojection_error_px: number;
}

/**
 * Universal WebSocket Envelope for M.I.R.A. Daemon Event Router
 */
export type EventType =
  | 'gesture'
  | 'speech'
  | 'media'
  | 'command'
  | 'telemetry'
  | 'wand'
  | 'calibration';

export interface MiraEventEnvelope<T = unknown> {
  topic: EventType | string;
  event: string;
  sender_node: string;
  timestamp: number;
  data: T;
}

export type EventKind =
  | 'EVENT'
  | 'COMMAND'
  | 'CAPABILITY_REQUEST'
  | 'CAPABILITY_RECEIPT'
  | 'ACK'
  | 'ERROR'
  | 'REGISTER'
  | 'REGISTER_ACK'
  | 'DISCONNECT';

export type AuthMode = 'LOOPBACK_TRUSTED' | 'MTLS';

export interface AuthContext {
  node_id: string;
  auth_mode: AuthMode;
  token: string | null;
  cert_fingerprint: string | null;
}

export interface MiraEnvelopeV2<T = unknown> {
  protocol_version: number;
  event_id: string;
  trace_id: string;
  topic: string;
  event_kind: EventKind;
  sender_node: string;
  timestamp: number;
  auth_context: AuthContext;
  payload: T;
  ack_ref: string | null;
}

export type Reversibility = 'REVERSIBLE' | 'IRREVERSIBLE' | 'UNKNOWN';

export interface CapabilityRequest {
  request_id: string;
  trace_id: string;
  sender_node: string;
  capability: string;
  arguments: Record<string, unknown>;
  reversibility: Reversibility;
  requested_at: number;
}

export interface ActionManifest {
  capability: string;
  reversibility: Reversibility;
  rollback_plan: string | null;
  credential_scope: string[];
  risk_notes: string;
}

export type CapabilityResultStatus = 'SUCCESS' | 'FAILURE' | 'DENIED' | 'ROLLED_BACK';

export interface CapabilityReceipt {
  request_id: string;
  capability: string;
  result_status: CapabilityResultStatus;
  rollback_evidence: string | null;
  executed_at: number;
  executed_by: string;
  signature: string | null;
}
