"""External identity provider: private key lives in AWS KMS.

Demonstrates the `IdentityProvider` protocol for integrators whose
signing key cannot leave an HSM / KMS boundary. The private key never
enters the SDK process; the SDK only holds the public key bytes and
relies on the external provider for signing.

This example stubs the KMS round-trip for readability. In production,
swap in `boto3.client("kms").sign(KeyId=..., Message=..., ...)` or the
equivalent for your provider.

Install::

    pip install checkrd openai
    # For real KMS use: pip install boto3

Run::

    python custom_identity_kms.py
"""
from __future__ import annotations

import os

import checkrd
from checkrd.identity import ExternalIdentity
from openai import OpenAI


# Replace with: boto3.client("kms").get_public_key(KeyId=...)["PublicKey"]
# (strip the DER/PKIX framing; Ed25519 public key is 32 raw bytes)
PUBLIC_KEY_BYTES = bytes(32)  # placeholder


def main() -> None:
    identity = ExternalIdentity(
        public_key=PUBLIC_KEY_BYTES,
        # instance_id deterministically derived from the public key;
        # override here if you want a stable server-side name.
    )

    # The SDK signs telemetry batches out-of-band via the external
    # signer; pass `identity=` to override the default LocalIdentity.
    checkrd.init(policy="policy.yaml", identity=identity)
    checkrd.instrument()

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Hello in five words."}],
    )
    checkrd.shutdown()


if __name__ == "__main__":
    main()
