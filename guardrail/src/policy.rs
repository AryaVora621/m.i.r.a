use std::collections::HashMap;

use mira_protocol::{ActionManifest, CapabilityRequest, Reversibility};

#[derive(Debug, Clone, PartialEq)]
pub enum PolicyDecision {
    Allow { manifest: ActionManifest },
    Deny { reason: String },
}

#[derive(Clone)]
pub struct PolicyTable {
    manifests: HashMap<String, ActionManifest>,
}

impl PolicyTable {
    pub fn new() -> Self {
        Self { manifests: HashMap::new() }
    }

    pub fn register(&mut self, manifest: ActionManifest) {
        self.manifests.insert(manifest.capability.clone(), manifest);
    }

    pub fn dev_default() -> Self {
        let mut table = Self::new();
        table.register(ActionManifest {
            capability: "mac.volume.set".to_string(),
            reversibility: Reversibility::Reversible,
            rollback_plan: Some(
                "Restore the previous system output volume level captured before this action executed."
                    .to_string(),
            ),
            credential_scope: vec!["mac.audio".to_string()],
            risk_notes: "Adjusts system output volume only; no data loss or irreversible side effects."
                .to_string(),
        });
        table
    }

    pub fn evaluate(&self, request: &CapabilityRequest) -> PolicyDecision {
        let Some(manifest) = self.manifests.get(&request.capability) else {
            return PolicyDecision::Deny {
                reason: format!("capability '{}' is not registered", request.capability),
            };
        };

        if manifest.reversibility != request.reversibility {
            return PolicyDecision::Deny {
                reason: "request reversibility does not match the registered action manifest".to_string(),
            };
        }

        match manifest.reversibility {
            Reversibility::Reversible => {
                if manifest.rollback_plan.is_none() {
                    return PolicyDecision::Deny {
                        reason: "reversible capability has no rollback plan on file".to_string(),
                    };
                }
            }
            Reversibility::Irreversible | Reversibility::Unknown => {
                return PolicyDecision::Deny {
                    reason: "only reversible actions with a tested rollback may run autonomously".to_string(),
                };
            }
        }

        PolicyDecision::Allow { manifest: manifest.clone() }
    }
}

impl Default for PolicyTable {
    fn default() -> Self {
        Self::new()
    }
}
