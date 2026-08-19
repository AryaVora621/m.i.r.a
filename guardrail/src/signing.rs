use std::fs;
use std::path::Path;

use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand::rngs::OsRng;

use mira_protocol::CapabilityReceipt;

pub struct DevSigner {
    signing_key: SigningKey,
}

impl DevSigner {
    // Dev-only key custody: generated on first run and persisted at `path`
    // so repeated local runs sign with the same key. Production key
    // custody for the root-owned signed guardrail binary is a later plan.
    pub fn load_or_generate(path: &Path) -> std::io::Result<Self> {
        if let Ok(bytes) = fs::read(path) {
            let key_bytes: [u8; 32] = bytes
                .try_into()
                .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "bad key length"))?;
            return Ok(Self { signing_key: SigningKey::from_bytes(&key_bytes) });
        }
        let signing_key = SigningKey::generate(&mut OsRng);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, signing_key.to_bytes())?;
        Ok(Self { signing_key })
    }

    pub fn verifying_key(&self) -> VerifyingKey {
        self.signing_key.verifying_key()
    }

    pub fn sign(&self, mut receipt: CapabilityReceipt) -> CapabilityReceipt {
        receipt.signature = None;
        let canonical = serde_json::to_vec(&receipt).expect("receipt always serializes");
        let signature: Signature = self.signing_key.sign(&canonical);
        receipt.signature = Some(STANDARD.encode(signature.to_bytes()));
        receipt
    }

    pub fn verify(&self, receipt: &CapabilityReceipt) -> bool {
        let Some(sig_b64) = &receipt.signature else { return false };
        let Ok(sig_bytes) = STANDARD.decode(sig_b64) else { return false };
        let Ok(signature) = Signature::from_slice(&sig_bytes) else { return false };
        let mut unsigned = receipt.clone();
        unsigned.signature = None;
        let canonical = match serde_json::to_vec(&unsigned) {
            Ok(bytes) => bytes,
            Err(_) => return false,
        };
        self.verifying_key().verify(&canonical, &signature).is_ok()
    }
}
