import { describe, expect, it } from "vitest";

import {
  LocalIdentity,
  ExternalIdentity,
  DEFAULT_KEY_ENV_VAR,
} from "../src/identity.js";
import { CheckrdInitError } from "../src/exceptions.js";

describe("LocalIdentity", () => {
  it("generates a fresh keypair", () => {
    const id = LocalIdentity.generate();
    expect(id.privateKeyBytes.byteLength).toBe(32);
    expect(id.publicKey.byteLength).toBe(32);
    expect(id.instanceId).toMatch(/^[0-9a-f]{16}$/);
  });

  it("derives publicKey from privateKey bytes via WASM", () => {
    const a = LocalIdentity.generate();
    const b = LocalIdentity.fromBytes(a.privateKeyBytes);
    expect(Array.from(b.publicKey)).toEqual(Array.from(a.publicKey));
    expect(b.instanceId).toBe(a.instanceId);
  });

  it("fromEnv reads a base64 private key", () => {
    const source = LocalIdentity.generate();
    const b64 = Buffer.from(source.privateKeyBytes).toString("base64");
    process.env[DEFAULT_KEY_ENV_VAR] = b64;
    try {
      const loaded = LocalIdentity.fromEnv();
      expect(Array.from(loaded.privateKeyBytes)).toEqual(
        Array.from(source.privateKeyBytes),
      );
    } finally {
      delete process.env[DEFAULT_KEY_ENV_VAR];
    }
  });

  it("fromEnv throws when the variable is unset", () => {
    delete process.env[DEFAULT_KEY_ENV_VAR];
    expect(() => LocalIdentity.fromEnv()).toThrow(CheckrdInitError);
  });

  it("fromBytes rejects non-32-byte keys", () => {
    expect(() => LocalIdentity.fromBytes(new Uint8Array(16)))
      .toThrow(CheckrdInitError);
  });
});

describe("ExternalIdentity", () => {
  it("exposes a null privateKeyBytes", () => {
    const pub = new Uint8Array(32);
    const id = new ExternalIdentity({ publicKey: pub });
    expect(id.privateKeyBytes).toBe(null);
    expect(id.instanceId).toMatch(/^[0-9a-f]{16}$/);
  });

  it("honors an explicit instanceId", () => {
    const id = new ExternalIdentity({
      publicKey: new Uint8Array(32),
      instanceId: "my-kms-instance1",
    });
    expect(id.instanceId).toBe("my-kms-instance1");
  });

  it("rejects bad public-key lengths", () => {
    expect(
      () => new ExternalIdentity({ publicKey: new Uint8Array(16) }),
    ).toThrow(CheckrdInitError);
  });
});
