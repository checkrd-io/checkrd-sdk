import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";

import {
  verifyWebhook,
  verifyWebhookAsync,
  WebhookVerificationError,
} from "../src/webhooks.js";

function sign(body: string, secret: string, timestamp: number): string {
  const payload = `${timestamp.toString()}.${body}`;
  const hex = createHmac("sha256", secret).update(payload).digest("hex");
  return `t=${timestamp.toString()},v1=${hex}`;
}

describe("verifyWebhook", () => {
  const SECRET = "whsec_0123456789abcdef";
  const NOW = 1_700_000_000;
  const body = JSON.stringify({ event: "policy.updated", version: 3 });

  it("returns silently for a valid signature", () => {
    const header = sign(body, SECRET, NOW);
    expect(() => {
      verifyWebhook({
        rawBody: body,
        signatureHeader: header,
        secret: SECRET,
        nowUnixSecs: () => NOW,
      });
    }).not.toThrow();
  });

  it("accepts Uint8Array body and string body interchangeably", () => {
    const header = sign(body, SECRET, NOW);
    verifyWebhook({
      rawBody: new TextEncoder().encode(body),
      signatureHeader: header,
      secret: SECRET,
      nowUnixSecs: () => NOW,
    });
  });

  it("throws signature_mismatch when the secret is wrong", () => {
    const header = sign(body, SECRET, NOW);
    try {
      verifyWebhook({
        rawBody: body,
        signatureHeader: header,
        secret: "wrong-secret",
        nowUnixSecs: () => NOW,
      });
      expect.fail("expected to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(WebhookVerificationError);
      expect((err as WebhookVerificationError).code).toBe("signature_mismatch");
    }
  });

  it("throws signature_mismatch when the body is tampered with", () => {
    const header = sign(body, SECRET, NOW);
    try {
      verifyWebhook({
        rawBody: `${body}EXTRA`,
        signatureHeader: header,
        secret: SECRET,
        nowUnixSecs: () => NOW,
      });
      expect.fail("expected to throw");
    } catch (err) {
      expect((err as WebhookVerificationError).code).toBe("signature_mismatch");
    }
  });

  it("throws timestamp_out_of_range when the header is stale", () => {
    const header = sign(body, SECRET, NOW);
    try {
      verifyWebhook({
        rawBody: body,
        signatureHeader: header,
        secret: SECRET,
        nowUnixSecs: () => NOW + 3600,
      });
      expect.fail("expected to throw");
    } catch (err) {
      expect((err as WebhookVerificationError).code).toBe("timestamp_out_of_range");
    }
  });

  it("throws malformed_header when the header is garbled", () => {
    try {
      verifyWebhook({
        rawBody: body,
        signatureHeader: "not-a-signature",
        secret: SECRET,
      });
      expect.fail("expected to throw");
    } catch (err) {
      expect((err as WebhookVerificationError).code).toBe("malformed_header");
    }
  });

  it("throws missing_header when the header is null or empty", () => {
    for (const hdr of [null, ""]) {
      try {
        verifyWebhook({
          rawBody: body,
          signatureHeader: hdr,
          secret: SECRET,
        });
        expect.fail("expected to throw");
      } catch (err) {
        expect((err as WebhookVerificationError).code).toBe("missing_header");
      }
    }
  });

  it("throws empty_secret when the secret is empty", () => {
    const header = sign(body, SECRET, NOW);
    try {
      verifyWebhook({
        rawBody: body,
        signatureHeader: header,
        secret: "",
      });
      expect.fail("expected to throw");
    } catch (err) {
      expect((err as WebhookVerificationError).code).toBe("empty_secret");
    }
  });

  it("accepts either secret during a rotation window", () => {
    const OLD = "whsec_old_key_0123456789";
    const NEW = "whsec_new_key_abcdef0123";
    const headerOld = sign(body, OLD, NOW);
    const headerNew = sign(body, NEW, NOW);
    for (const hdr of [headerOld, headerNew]) {
      verifyWebhook({
        rawBody: body,
        signatureHeader: hdr,
        secret: [OLD, NEW],
        nowUnixSecs: () => NOW,
      });
    }
  });
});

describe("verifyWebhookAsync", () => {
  it("verifies valid signatures via Web Crypto path", async () => {
    const SECRET = "whsec_async_test";
    const NOW = 1_700_000_000;
    const body = "ping";
    const header = sign(body, SECRET, NOW);
    await expect(
      verifyWebhookAsync({
        rawBody: body,
        signatureHeader: header,
        secret: SECRET,
        nowUnixSecs: () => NOW,
      }),
    ).resolves.toBeUndefined();
  });

  it("rejects bad signatures via Web Crypto path", async () => {
    const SECRET = "whsec_async_test";
    const NOW = 1_700_000_000;
    const body = "ping";
    const header = sign(body, "different-secret", NOW);
    await expect(
      verifyWebhookAsync({
        rawBody: body,
        signatureHeader: header,
        secret: SECRET,
        nowUnixSecs: () => NOW,
      }),
    ).rejects.toBeInstanceOf(WebhookVerificationError);
  });
});
