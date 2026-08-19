"""
Unit tests for M.I.R.A. JSON Schemas and Python Typed Models.
Validates draft-07 compliance, Pydantic model serialization, and payload validation.
"""

import json
import os
import unittest
import jsonschema
from jsonschema import Draft7Validator

from shared.types.events import (
    ActionManifest,
    AuthContext,
    AuthMode,
    CalibrationEvent,
    CalibrationStage,
    CapabilityReceipt,
    CapabilityRequest,
    CapabilityResultStatus,
    Coordinates3D,
    EventKind,
    GestureEvent,
    GestureType,
    LyricLine,
    MediaEvent,
    MiraEnvelopeV2,
    NodeCommand,
    Point2D,
    Reversibility,
    SpeechEvent,
    SpeechState,
    TelemetryEvent,
    TelemetryMetrics,
    TelemetryStatus,
    WandButton,
    WandEvent,
    WandEventType,
)


class TestSchemasAndModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas"
        )
        cls.schemas = {}
        for filename in os.listdir(cls.schema_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(cls.schema_dir, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    schema_data = json.load(f)
                    cls.schemas[filename] = schema_data
                    # Check draft-7 compliance
                    Draft7Validator.check_schema(schema_data)
        cls.fixture_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"
        )

    @classmethod
    def _load_fixture(cls, filename):
        with open(os.path.join(cls.fixture_dir, filename), "r", encoding="utf-8") as f:
            return json.load(f)

    def test_schema_files_exist(self):
        expected_schemas = {
            "gesture_event.json",
            "speech_event.json",
            "media_event.json",
            "node_command.json",
            "telemetry_event.json",
            "wand_event.json",
            "calibration_event.json",
            "mira_envelope_v2.json",
            "capability_request.json",
            "action_manifest.json",
            "capability_receipt.json",
        }
        self.assertTrue(expected_schemas.issubset(set(self.schemas.keys())))

    def test_gesture_event(self):
        schema = self.schemas["gesture_event.json"]
        payload = {
            "node_id": "pi-projector",
            "timestamp": 1723891234.56,
            "gesture_type": "POINT",
            "coordinates": {"x": 0.45, "y": 0.62, "z": -0.12},
            "confidence": 0.98,
        }
        # Validate against JSON schema
        jsonschema.validate(instance=payload, schema=schema)

        # Validate with Pydantic
        model = GestureEvent(**payload)
        self.assertEqual(model.gesture_type, GestureType.POINT)
        self.assertEqual(model.coordinates.x, 0.45)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # Invalid gesture_type
        invalid_payload = dict(payload, gesture_type="INVALID_MOVE")
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_payload, schema=schema)
        with self.assertRaises(Exception):
            GestureEvent(**invalid_payload)

        # Invalid confidence (> 1.0)
        invalid_conf = dict(payload, confidence=1.5)
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_conf, schema=schema)
        with self.assertRaises(Exception):
            GestureEvent(**invalid_conf)

    def test_speech_event(self):
        schema = self.schemas["speech_event.json"]
        payload = {
            "node_id": "mac-host",
            "timestamp": 1723891235.12,
            "state": "LISTENING",
            "raw_transcript": "jarvis turn on desk lamp",
            "cleaned_prompt": "turn on desk lamp",
            "is_final": True,
        }
        jsonschema.validate(instance=payload, schema=schema)
        model = SpeechEvent(**payload)
        self.assertEqual(model.state, SpeechState.LISTENING)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # Missing required field
        invalid_payload = dict(payload)
        del invalid_payload["state"]
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_payload, schema=schema)
        with self.assertRaises(Exception):
            SpeechEvent(**invalid_payload)

    def test_media_event(self):
        schema = self.schemas["media_event.json"]
        payload_with_lyrics = {
            "node_id": "mac-host",
            "timestamp": 1723891236.0,
            "track": "Starboy",
            "artist": "The Weeknd",
            "album": "Starboy",
            "duration_ms": 230000,
            "position_ms": 45000,
            "is_playing": True,
            "lyrics": [
                {"time_ms": 0, "text": "I'm tryna put you in the worst mood, ah"},
                {"time_ms": 3000, "text": "P1 cleaner than your church shoes, ah"},
            ],
        }
        jsonschema.validate(instance=payload_with_lyrics, schema=schema)
        model = MediaEvent(**payload_with_lyrics)
        self.assertEqual(len(model.lyrics), 2)
        self.assertEqual(model.model_dump(mode="json"), payload_with_lyrics)

        # Without lyrics
        payload_no_lyrics = {
            "node_id": "mac-host",
            "timestamp": 1723891236.0,
            "track": "Blinding Lights",
            "artist": "The Weeknd",
            "album": "After Hours",
            "duration_ms": 200000,
            "position_ms": 10000,
            "is_playing": False,
        }
        jsonschema.validate(instance=payload_no_lyrics, schema=schema)
        model2 = MediaEvent(**payload_no_lyrics)
        self.assertIsNone(model2.lyrics)

    def test_node_command(self):
        schema = self.schemas["node_command.json"]
        payload = {
            "command_id": "cmd-123e4567-e89b-12d3-a456-426614174000",
            "target_node": "mac-host",
            "action": "set_volume",
            "payload": {"level": 75},
            "timeout_ms": 2000,
        }
        jsonschema.validate(instance=payload, schema=schema)
        model = NodeCommand(**payload)
        self.assertEqual(model.action, "set_volume")
        self.assertEqual(model.payload["level"], 75)
        self.assertEqual(model.model_dump(mode="json"), payload)

    def test_telemetry_event(self):
        schema = self.schemas["telemetry_event.json"]
        payload = {
            "node_id": "homelab-p40",
            "timestamp": 1723891238.0,
            "status": "OK",
            "metrics": {
                "cpu_percent": 14.5,
                "vram_used_gb": 8.2,
                "vram_total_gb": 24.0,
                "fps": 60.0,
                "temp_c": 54.0,
            },
        }
        jsonschema.validate(instance=payload, schema=schema)
        model = TelemetryEvent(**payload)
        self.assertEqual(model.status, TelemetryStatus.OK)
        self.assertEqual(model.metrics.vram_total_gb, 24.0)
        self.assertEqual(model.model_dump(mode="json"), payload)

    def test_wand_event(self):
        schema = self.schemas["wand_event.json"]
        payload = {
            "event": "button_down",
            "button": "ptt",
            "gpio": 1,
            "timestamp_ms": 1723891239100,
        }
        jsonschema.validate(instance=payload, schema=schema)
        model = WandEvent(**payload)
        self.assertEqual(model.event, WandEventType.BUTTON_DOWN)
        self.assertEqual(model.button, WandButton.PTT)
        self.assertEqual(model.gpio, 1)

        payload_with_snap = {
            "event": "inspect_snap",
            "button": "shutter",
            "gpio": 43,
            "timestamp_ms": 1723891240000,
            "payload": "base64_jpeg_preview_data...",
        }
        jsonschema.validate(instance=payload_with_snap, schema=schema)
        model_snap = WandEvent(**payload_with_snap)
        self.assertEqual(model_snap.event, WandEventType.INSPECT_SNAP)
        self.assertEqual(model_snap.model_dump(mode="json"), payload_with_snap)

    def test_calibration_event(self):
        schema = self.schemas["calibration_event.json"]
        payload = {
            "node_id": "pi-projector",
            "timestamp": 1723891241.0,
            "stage": "COMPUTED",
            "corners": [
                {"x": 96.0, "y": 54.0},
                {"x": 1824.0, "y": 54.0},
                {"x": 1824.0, "y": 1026.0},
                {"x": 96.0, "y": 1026.0},
            ],
            "homography_matrix": [
                [1.2451, -0.0123, -45.2],
                [0.0054, 1.1892, -32.1],
                [0.00001, -0.00003, 1.0],
            ],
            "reprojection_error_px": 0.84,
        }
        jsonschema.validate(instance=payload, schema=schema)
        model = CalibrationEvent(**payload)
        self.assertEqual(model.stage, CalibrationStage.COMPUTED)
        self.assertEqual(len(model.corners), 4)
        self.assertAlmostEqual(model.reprojection_error_px, 0.84)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # Invalid corners length (e.g. 3 corners instead of 4)
        invalid_corners = dict(payload, corners=[{"x": 0.0, "y": 0.0}] * 3)
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_corners, schema=schema)
        with self.assertRaises(Exception):
            CalibrationEvent(**invalid_corners)

    def test_mira_envelope_v2(self):
        schema = self.schemas["mira_envelope_v2.json"]
        payload = self._load_fixture("mira_envelope_v2.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = MiraEnvelopeV2(**payload)
        self.assertEqual(model.event_kind, EventKind.CAPABILITY_REQUEST)
        self.assertEqual(model.auth_context.auth_mode, AuthMode.LOOPBACK_TRUSTED)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # protocol_version must be exactly 2
        invalid_version = dict(payload, protocol_version=1)
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_version, schema=schema)
        with self.assertRaises(Exception):
            MiraEnvelopeV2(**invalid_version)

        # unknown event_kind is rejected
        invalid_kind = dict(payload, event_kind="UNKNOWN_KIND")
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_kind, schema=schema)
        with self.assertRaises(Exception):
            MiraEnvelopeV2(**invalid_kind)

    def test_capability_request(self):
        schema = self.schemas["capability_request.json"]
        payload = self._load_fixture("capability_request.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = CapabilityRequest(**payload)
        self.assertEqual(model.reversibility, Reversibility.REVERSIBLE)
        self.assertEqual(model.model_dump(mode="json"), payload)

        invalid_reversibility = dict(payload, reversibility="MAYBE")
        with self.assertRaises(Exception):
            jsonschema.validate(instance=invalid_reversibility, schema=schema)
        with self.assertRaises(Exception):
            CapabilityRequest(**invalid_reversibility)

    def test_action_manifest(self):
        schema = self.schemas["action_manifest.json"]
        payload = self._load_fixture("action_manifest.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = ActionManifest(**payload)
        self.assertEqual(model.credential_scope, ["mac.audio"])
        self.assertEqual(model.model_dump(mode="json"), payload)

    def test_capability_receipt(self):
        schema = self.schemas["capability_receipt.json"]
        payload = self._load_fixture("capability_receipt.json")
        jsonschema.validate(instance=payload, schema=schema)
        model = CapabilityReceipt(**payload)
        self.assertEqual(model.result_status, CapabilityResultStatus.SUCCESS)
        self.assertEqual(model.model_dump(mode="json"), payload)

        # a DENIED receipt must still validate with a null rollback_evidence
        denied_payload = dict(
            payload,
            result_status="DENIED",
            rollback_evidence=None,
            signature=None,
        )
        jsonschema.validate(instance=denied_payload, schema=schema)
        denied_model = CapabilityReceipt(**denied_payload)
        self.assertIsNone(denied_model.rollback_evidence)


if __name__ == "__main__":
    unittest.main()
