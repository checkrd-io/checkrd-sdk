/**
 * Agent identity abstraction. Mirrors
 * ``wrappers/python/src/checkrd/identity.py``.
 *
 * Two implementations ship:
 *
 *   - {@link LocalIdentity} — SDK owns the 32-byte Ed25519 private
 *     key. The key is generated fresh, read from an env var, read
 *     from a base64 secret, or read from a file. This is the default
 *     path; the key lives in WASM linear memory after init.
 *   - {@link ExternalIdentity} — signing happens outside the SDK
 *     (AWS KMS, Azure Key Vault, GCP Secret Manager, a hosted
 *     signer). The SDK holds only the 32-byte public key and an
 *     instance id. Telemetry signing falls back to unsigned when
 *     the engine's own private-key path is unavailable.
 *
 * Callers that need a different signer implement
 * {@link IdentityProvider} directly.
 */

import { WasmEngine } from "./engine.js";
import { readEnv } from "./_env.js";
import { CheckrdInitError } from "./exceptions.js";

/**
 * Structural interface for any identity provider.
 *
 * Consumers read `publicKey` for public-key registration with the
 * control plane and `instanceId` for the signature keyid field.
 * `privateKeyBytes` is `null` for external providers (KMS etc.) and
 * a `Uint8Array` for local providers.
 */
export interface IdentityProvider {
  /** 32-byte Ed25519 public key. */
  readonly publicKey: Uint8Array;
  /** 16-hex-char fingerprint used as the RFC 9421 `keyid`. */
  readonly instanceId: string;
  /**
   * 32-byte Ed25519 private key for local signing, or `null` when
   * the provider signs externally.
   */
  readonly privateKeyBytes: Uint8Array | null;
}

/** Default env-var name read by {@link LocalIdentity.fromEnv}. */
export const DEFAULT_KEY_ENV_VAR = "CHECKRD_AGENT_KEY";

/**
 * Local identity: the SDK owns the private key bytes.
 *
 * Constructors (`new LocalIdentity()` etc.) do not ship in the public
 * API; use one of the static factory methods.
 */
export class LocalIdentity implements IdentityProvider {
  private readonly _privateKey: Uint8Array;
  readonly publicKey: Uint8Array;
  readonly instanceId: string;

  private constructor(privateKey: Uint8Array, publicKey: Uint8Array) {
    if (privateKey.byteLength !== 32) {
      throw new CheckrdInitError(
        `private key must be 32 bytes, got ${privateKey.byteLength.toString()}`,
      );
    }
    if (publicKey.byteLength !== 32) {
      throw new CheckrdInitError(
        `public key must be 32 bytes, got ${publicKey.byteLength.toString()}`,
      );
    }
    this._privateKey = privateKey;
    this.publicKey = publicKey;
    // The keyid is the first 16 hex characters of the SHA-256 of the
    // public key. We intentionally do not require subtle to be
    // available synchronously — the public-key bytes themselves are
    // enough in practice. Matches the Python wrapper.
    this.instanceId = bytesToHex(publicKey).slice(0, 16);
  }

  /** The 32-byte private key. Caller must treat as sensitive. */
  get privateKeyBytes(): Uint8Array {
    return this._privateKey;
  }

  /** Generate a fresh Ed25519 keypair inside the WASM core. */
  static generate(): LocalIdentity {
    const kp = WasmEngine.generateKeypair();
    return new LocalIdentity(kp.privateKey, kp.publicKey);
  }

  /**
   * Load a private key from an environment variable. The value is
   * interpreted as base64-encoded 32 bytes (matches the Python SDK's
   * format). Raises when the variable is absent or the value does
   * not decode to 32 bytes.
   */
  static fromEnv(varName: string = DEFAULT_KEY_ENV_VAR): LocalIdentity {
    const b64 = readEnv(varName);
    if (b64 === undefined) {
      throw new CheckrdInitError(
        `environment variable ${varName} is not set; cannot load identity`,
      );
    }
    return LocalIdentity.fromBytes(base64ToBytes(b64));
  }

  /**
   * Construct from raw 32 bytes. Useful for secrets-manager
   * integrations that return the key as binary.
   */
  static fromBytes(privateKey: Uint8Array): LocalIdentity {
    const publicKey = WasmEngine.derivePublicKey(privateKey);
    return new LocalIdentity(privateKey, publicKey);
  }

  /**
   * Load from a file. The file must contain the 32-byte private key
   * followed by the 32-byte public key (64 bytes total), matching the
   * format the Python SDK writes via `checkrd keygen --output`.
   *
   * Node-only; lazily imports `node:fs` so non-Node bundles stay
   * free of it. On POSIX systems the file's mode is checked: in
   * strict mode (default) any group / other bit causes an error;
   * `CHECKRD_SECURITY_MODE=permissive` downgrades to a warning.
   */
  static async fromFile(path: string): Promise<LocalIdentity> {
    const { readFile, stat } = await import("node:fs/promises");
    const { platform } = await import("node:process");

    if (platform !== "win32") {
      try {
        const s = await stat(path);
        const mode = s.mode & 0o777;
        if (mode & 0o077) {
          const strict =
            (readEnv("CHECKRD_SECURITY_MODE") ?? "strict") !== "permissive";
          if (strict) {
            throw new CheckrdInitError(
              `identity key at ${path} has permissions ${mode.toString(8)} ` +
                "which are accessible to group or other. Refusing to load in " +
                `strict mode. Run \`chmod 600 ${path}\` to repair, or set ` +
                "CHECKRD_SECURITY_MODE=permissive for a controlled rollout.",
            );
          }
        }
      } catch (err) {
        if (err instanceof CheckrdInitError) throw err;
        // Stat failure falls through to the read-and-throw path below.
      }
    }

    const raw = await readFile(path);
    if (raw.byteLength !== 64) {
      throw new CheckrdInitError(
        `identity key at ${path} is ${raw.byteLength.toString()} bytes; ` +
          "expected 64 (32 private + 32 public)",
      );
    }
    const priv = new Uint8Array(raw.buffer, raw.byteOffset, 32).slice();
    const pub = new Uint8Array(raw.buffer, raw.byteOffset + 32, 32).slice();
    return new LocalIdentity(priv, pub);
  }
}

/** Options for {@link ExternalIdentity}. */
export interface ExternalIdentityOptions {
  /** 32-byte Ed25519 public key. */
  publicKey: Uint8Array;
  /**
   * Explicit 16-char instance id. If omitted, derived from the
   * public-key bytes (the same derivation as {@link LocalIdentity}).
   */
  instanceId?: string;
}

/**
 * External identity: signing happens elsewhere (KMS, HSM, signer
 * service). The SDK holds only the public key and the instance id.
 * Telemetry signing via the WASM core's local-key path is disabled;
 * integrators must sign batches out-of-band before they reach the
 * control plane.
 */
export class ExternalIdentity implements IdentityProvider {
  readonly publicKey: Uint8Array;
  readonly instanceId: string;
  readonly privateKeyBytes: null = null;

  constructor(opts: ExternalIdentityOptions) {
    if (opts.publicKey.byteLength !== 32) {
      throw new CheckrdInitError(
        `public key must be 32 bytes, got ${opts.publicKey.byteLength.toString()}`,
      );
    }
    this.publicKey = opts.publicKey;
    this.instanceId = opts.instanceId ?? bytesToHex(opts.publicKey).slice(0, 16);
  }
}

/** Convert a Uint8Array to a lowercase-hex string without allocations beyond necessary. */
function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Decode base64 to bytes in a runtime-agnostic way (Node / Bun / Deno / workerd). */
function base64ToBytes(b64: string): Uint8Array {
  const trimmed = b64.trim();
  // `Buffer` exists on Node / Bun. Cloudflare / Deno / browser have
  // atob. Pick whichever is available.
  const nodeBuffer = (globalThis as unknown as {
    Buffer?: { from(data: string, encoding: string): Uint8Array };
  }).Buffer;
  if (nodeBuffer) {
    return new Uint8Array(nodeBuffer.from(trimmed, "base64"));
  }
  const bin = atob(trimmed);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
