import mira_envelope_v2 from '../fixtures/mira_envelope_v2.json';
import capability_request from '../fixtures/capability_request.json';
import action_manifest from '../fixtures/action_manifest.json';
import capability_receipt from '../fixtures/capability_receipt.json';
import type { MiraEnvelopeV2, CapabilityRequest, ActionManifest, CapabilityReceipt } from './index';

const envelope: MiraEnvelopeV2 = mira_envelope_v2 as MiraEnvelopeV2;
const request: CapabilityRequest = capability_request as CapabilityRequest;
const manifest: ActionManifest = action_manifest as ActionManifest;
const receipt: CapabilityReceipt = capability_receipt as CapabilityReceipt;

void envelope;
void request;
void manifest;
void receipt;
