import mira_envelope_v2 from '../fixtures/mira_envelope_v2.json';
import capability_request from '../fixtures/capability_request.json';
import action_manifest from '../fixtures/action_manifest.json';
import capability_receipt from '../fixtures/capability_receipt.json';
import type { MiraEnvelopeV2, CapabilityRequest, ActionManifest, CapabilityReceipt, EventKind, AuthMode, Reversibility, CapabilityResultStatus } from './index';

const envelope: MiraEnvelopeV2 = {
  ...mira_envelope_v2,
  event_kind: mira_envelope_v2.event_kind as EventKind,
  auth_context: {
    ...mira_envelope_v2.auth_context,
    auth_mode: mira_envelope_v2.auth_context.auth_mode as AuthMode,
  },
};

const request: CapabilityRequest = {
  ...capability_request,
  reversibility: capability_request.reversibility as Reversibility,
};

const manifest: ActionManifest = {
  ...action_manifest,
  reversibility: action_manifest.reversibility as Reversibility,
};

const receipt: CapabilityReceipt = {
  ...capability_receipt,
  result_status: capability_receipt.result_status as CapabilityResultStatus,
};

void envelope;
void request;
void manifest;
void receipt;
