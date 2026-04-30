"""Pluggable identity providers for agent authentication.

The WASM core handles policy evaluation and telemetry event creation.
Identity providers handle key management and cryptographic signing for
telemetry authentication. There are three deployment modes:

- **Dev (default)**: ``LocalIdentity()`` auto-generates an Ed25519 keypair at
  ``~/.checkrd/identity.key`` on first run. Convenient for the "first 10
  minutes" experience.
- **Production with cloud control plane**: generate the key once with
  ``checkrd keygen``, distribute via your secrets manager (AWS Secrets
  Manager / Vault / k8s Secrets / SOPS / 1Password), and load it explicitly
  via ``LocalIdentity.from_env()``, ``LocalIdentity.from_file()``, or
  ``LocalIdentity.from_bytes()``. The same key is shared by all replicas of
  the agent — auto-registration is idempotent for matching keys.
- **External (KMS/HSM)**: subclass ``ExternalIdentity``. The private key
  never leaves the secure enclave; signing happens via the provider's API.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, Union, runtime_checkable

from checkrd.exceptions import CheckrdInitError

if TYPE_CHECKING:
    from checkrd.engine import WasmEngine

logger = logging.getLogger("checkrd")

_KEY_FILE_SIZE = 64  # 32-byte private key + 32-byte public key
_KEY_LEN = 32

#: Default environment variable name read by ``LocalIdentity.from_env``.
DEFAULT_KEY_ENV_VAR = "CHECKRD_AGENT_KEY"


@runtime_checkable
class IdentityProvider(Protocol):
    """Protocol for pluggable agent identity.

    Implementations must provide key material and signing. The ``wrap()``
    function uses this interface to configure the WASM engine and sign
    outbound telemetry.

    For local keys, the private key is passed to the WASM core (which
    handles signing internally). For KMS/HSM, ``private_key_bytes``
    returns ``None`` and ``sign()`` calls the external signing API.
    """

    @property
    def private_key_bytes(self) -> Optional[bytes]:
        """Raw 32-byte Ed25519 private key, or None for external providers."""
        ...

    @property
    def public_key(self) -> bytes:
        """32-byte Ed25519 public key."""
        ...

    @property
    def instance_id(self) -> str:
        """Unique instance identifier derived from the public key."""
        ...

    def sign(self, payload: bytes) -> bytes:
        """Sign a payload, returning the Ed25519 signature bytes."""
        ...


def _default_key_path() -> Path:
    override = os.environ.get("CHECKRD_CONFIG_DIR")
    if override:
        return Path(override) / "identity.key"
    return Path.home() / ".checkrd" / "identity.key"


class LocalIdentity:
    """Ed25519 identity backed by a local key file.

    On first use, generates a keypair via the WASM core and stores both
    the private and public key (64 bytes) at ``key_path``. On subsequent
    uses, loads the existing key file.

    The private key is passed to the WASM core during ``init()``, which
    handles Ed25519 signing internally.

    Args:
        key_path: Path to the identity key file. Defaults to
            ``~/.checkrd/identity.key`` (respects ``CHECKRD_CONFIG_DIR``).
    """

    def __init__(self, key_path: Optional[Path] = None) -> None:
        self._key_path: Optional[Path] = key_path or _default_key_path()
        self._private_key: Optional[bytearray] = None  # bytearray so we can zeroize
        self._public_key: Optional[bytes] = None
        self._instance_id: Optional[str] = None
        self._engine: Optional[WasmEngine] = None  # set by bind_engine()
        self._zeroized: bool = False

    @classmethod
    def from_env(cls, var_name: str = DEFAULT_KEY_ENV_VAR) -> "LocalIdentity":
        """Load a private key from a base64-encoded environment variable.

        This is the recommended pattern for production deployments. Generate
        the key once with ``checkrd keygen``, put the private key in your
        secrets manager (AWS Secrets Manager / Vault / k8s Secret / etc.),
        and mount it as an environment variable. All replicas of the agent
        read the same value, so the auto-registration with the control plane
        is idempotent (the same public key is registered once, then re-
        registrations are no-ops).

        Args:
            var_name: Environment variable name. Defaults to
                ``CHECKRD_AGENT_KEY`` so simple deployments need no
                configuration beyond setting that one variable.

        Raises:
            CheckrdInitError: If the variable is unset, not valid base64,
                or the decoded value is not exactly 32 bytes.

        Example:
            >>> import os, base64
            >>> from checkrd import LocalIdentity
            >>> from checkrd.engine import WasmEngine
            >>> private, public = WasmEngine.generate_keypair()
            >>> os.environ["CHECKRD_AGENT_KEY"] = base64.b64encode(private).decode()
            >>> identity = LocalIdentity.from_env()
            >>> identity.public_key == public
            True
        """
        raw = os.environ.get(var_name)
        if not raw:
            raise CheckrdInitError(
                f"Environment variable {var_name} is not set. "
                f"Generate a key with `checkrd keygen` and export it: "
                f"`export {var_name}=$(checkrd keygen --private-only)`."
            )
        try:
            private_key = base64.b64decode(raw.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CheckrdInitError(
                f"Environment variable {var_name} is not valid base64: {exc}. "
                f"Expected the output of `checkrd keygen --private-only`."
            ) from exc
        return cls.from_bytes(private_key)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "LocalIdentity":
        """Load a private key from an explicit file path.

        Unlike the default ``LocalIdentity()`` constructor, this does NOT
        auto-generate a key when the file is missing. Production code should
        never silently fabricate an identity — that would mask key
        distribution bugs and violate ops expectations.

        The file format is the same 64-byte ``private+public`` blob written
        by the dev key file at ``~/.checkrd/identity.key``, so a dev key can
        be copied directly into production secrets storage if needed.

        Args:
            path: Path to a 64-byte key file (32 bytes private + 32 bytes public).

        Raises:
            CheckrdInitError: If the file does not exist, is the wrong size,
                or the stored public key does not match the private key.

        Example:
            >>> from pathlib import Path
            >>> from checkrd import LocalIdentity
            >>> from checkrd.engine import WasmEngine
            >>> import tempfile, os
            >>> private, public = WasmEngine.generate_keypair()
            >>> with tempfile.NamedTemporaryFile(delete=False) as f:
            ...     _ = f.write(private + public)
            ...     path = f.name
            >>> identity = LocalIdentity.from_file(path)
            >>> identity.public_key == public
            True
            >>> os.unlink(path)
        """
        from checkrd.engine import WasmEngine

        key_path = Path(path)
        if not key_path.exists():
            raise CheckrdInitError(
                f"Key file not found at {key_path}. "
                f"This constructor does NOT auto-generate keys. "
                f"Generate one with `checkrd keygen` and write it to this path, "
                f"or use the default LocalIdentity() constructor for dev."
            )
        data = key_path.read_bytes()
        if len(data) != _KEY_FILE_SIZE:
            raise CheckrdInitError(
                f"Invalid identity key at {key_path}: "
                f"expected {_KEY_FILE_SIZE} bytes (32 private + 32 public), "
                f"got {len(data)} bytes."
            )
        private_key = data[:_KEY_LEN]
        public_key = data[_KEY_LEN:]

        # Validate: derived public must match the stored public.
        derived = WasmEngine.derive_public_key(private_key)
        if derived != public_key:
            raise CheckrdInitError(
                f"Corrupt identity key at {key_path}: "
                f"stored public key does not match private key. "
                f"Regenerate with `checkrd keygen`."
            )

        instance = cls.__new__(cls)
        instance._key_path = key_path
        instance._private_key = bytearray(private_key)
        instance._public_key = public_key
        instance._instance_id = public_key[:8].hex()
        instance._engine = None
        instance._zeroized = False
        return instance

    @classmethod
    def from_bytes(cls, private_key: bytes) -> "LocalIdentity":
        """Construct from raw 32-byte private key bytes.

        Use this when loading the key from a secrets-manager SDK that
        returns binary content, e.g.::

            import boto3
            from checkrd import LocalIdentity, wrap

            response = boto3.client("secretsmanager").get_secret_value(
                SecretId="checkrd/sales-agent",
            )
            identity = LocalIdentity.from_bytes(response["SecretBinary"])
            client = wrap(httpx.Client(), agent_id="sales-agent",
                          identity=identity, ...)

        Args:
            private_key: Raw 32-byte Ed25519 private key.

        Raises:
            CheckrdInitError: If ``private_key`` is not exactly 32 bytes.

        Example:
            >>> from checkrd import LocalIdentity
            >>> from checkrd.engine import WasmEngine
            >>> private, public = WasmEngine.generate_keypair()
            >>> identity = LocalIdentity.from_bytes(private)
            >>> identity.public_key == public
            True
            >>> len(identity.instance_id)
            16
        """
        from checkrd.engine import WasmEngine

        if not isinstance(private_key, (bytes, bytearray)):
            raise CheckrdInitError(
                f"private_key must be bytes, got {type(private_key).__name__}"
            )
        if len(private_key) != _KEY_LEN:
            raise CheckrdInitError(
                f"Private key must be {_KEY_LEN} bytes, got {len(private_key)}. "
                f"This is a raw Ed25519 private key, not the 64-byte file format."
            )
        # Validate by deriving the public key. This also catches degenerate
        # private keys that the WASM engine would reject at sign time.
        try:
            public_key = WasmEngine.derive_public_key(bytes(private_key))
        except Exception as exc:  # noqa: BLE001 - WASM may raise various errors
            raise CheckrdInitError(
                f"Failed to derive public key from private key: {exc}"
            ) from exc

        instance = cls.__new__(cls)
        instance._key_path = None
        instance._private_key = bytearray(private_key)
        instance._public_key = public_key
        instance._instance_id = public_key[:8].hex()
        instance._engine = None
        instance._zeroized = False
        return instance

    def _ensure_loaded(self) -> None:
        """Load or generate key material on first access.

        For instances constructed via the new ``from_env``/``from_file``/
        ``from_bytes`` classmethods, this is a no-op (the key is already
        populated). For the default ``LocalIdentity()`` constructor, this
        loads or generates the dev key file at ``~/.checkrd/identity.key``.

        Uses write-then-link for atomic file creation (git/ssh-keygen pattern):
        1. Write key to a temp file in the same directory.
        2. os.link() to the target path (atomic, fails if target exists).
        3. If link succeeds: we won the race.
        4. If link fails: another process won; read their key.

        This avoids the O_EXCL empty-file race (where losers read a
        zero-byte file before the winner finishes writing).
        """
        if self._private_key is not None:
            return

        # If we got here without a key path, the instance was constructed via
        # __new__ (e.g. from_bytes) but the caller cleared the keys somehow.
        # This shouldn't happen in practice; raise loudly so the bug is visible.
        if self._key_path is None:
            raise CheckrdInitError(
                "LocalIdentity has no key and no key_path to load from. "
                "This is a bug — file an issue."
            )

        from checkrd.engine import WasmEngine
        import tempfile

        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        key_str = str(self._key_path)

        if self._key_path.exists():
            self._load_existing()
        else:
            # Generate key and write to temp file first.
            private, public = WasmEngine.generate_keypair()
            temp_fd, temp_path = tempfile.mkstemp(
                dir=str(self._key_path.parent),
                prefix=".identity_",
            )
            try:
                os.write(temp_fd, private + public)
                os.close(temp_fd)
                # Ensure restrictive permissions (0o600) before linking to
                # the final path — matches ssh-keygen behavior.
                os.chmod(temp_path, 0o600)
                # Atomic link: fails with OSError if target already exists.
                os.link(temp_path, key_str)
                # We won the race.
                self._private_key = bytearray(private)
                self._public_key = public
                logger.info("checkrd: generated identity key at %s", self._key_path)
            except OSError:
                # Another process created the file first -- read theirs.
                self._load_existing()
            finally:
                # Clean up temp file (link created a second reference if we won).
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        if self._private_key is None or self._public_key is None:
            # Initialization completed without populating key material —
            # impossible if the read/generate branches above succeeded.
            raise CheckrdInitError(
                "Internal: identity initialization finished without "
                "populating key material"
            )
        self._instance_id = self._public_key[:8].hex()

        # Validate: stored public key must match the private key.
        derived = WasmEngine.derive_public_key(bytes(self._private_key))
        if derived != self._public_key:
            raise CheckrdInitError(
                f"Corrupt identity key at {self._key_path}: "
                "stored public key does not match private key. "
                "Delete the file to regenerate."
            )

    def _load_existing(self) -> None:
        """Load and validate an existing key file."""
        if self._key_path is None:
            raise CheckrdInitError(
                "LocalIdentity._load_existing called without a key_path. "
                "This is a bug — file an issue."
            )
        # Warn if the key file has overly permissive permissions (world/group-readable).
        try:
            mode = self._key_path.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning(
                    "checkrd: identity key at %s has permissions %04o "
                    "(should be 0600). Run: chmod 600 %s",
                    self._key_path, mode, self._key_path,
                )
        except OSError:
            pass  # stat failed — we'll catch the real error on read below
        data = self._key_path.read_bytes()
        if len(data) != _KEY_FILE_SIZE:
            raise CheckrdInitError(
                f"Invalid identity key at {self._key_path}: "
                f"expected {_KEY_FILE_SIZE} bytes, got {len(data)}"
            )
        self._private_key = bytearray(data[:_KEY_LEN])
        self._public_key = data[_KEY_LEN:]

        self._instance_id = self._public_key[:8].hex()

    @property
    def private_key_bytes(self) -> Optional[bytes]:
        """The 32-byte Ed25519 private key.

        Returns None after ``bind_engine()`` has been called, because the
        key material has been zeroized from Python memory (the WASM core
        retains its own copy in isolated linear memory for signing).

        Returns an immutable ``bytes`` copy. For internal use where the
        mutable ``bytearray`` is needed (to avoid un-zeroizable copies),
        use :meth:`_private_key_ref` instead.
        """
        if self._zeroized:
            return None
        self._ensure_loaded()
        return bytes(self._private_key) if self._private_key is not None else None

    def _private_key_ref(self) -> Optional[bytearray]:
        """Return a direct reference to the private key bytearray.

        Internal. Unlike :attr:`private_key_bytes`, this does **not**
        create an immutable ``bytes`` copy — the returned ``bytearray``
        is the same object that :meth:`bind_engine` will later zeroize.
        This avoids leaving un-zeroizable copies on the Python heap.

        Callers must not store the reference beyond the immediate use.
        """
        if self._zeroized:
            return None
        self._ensure_loaded()
        return self._private_key

    @property
    def public_key(self) -> bytes:
        """The 32-byte Ed25519 public key."""
        self._ensure_loaded()
        if self._public_key is None:
            raise RuntimeError(
                "internal: _ensure_loaded() returned without setting public_key"
            )
        return self._public_key

    @property
    def instance_id(self) -> str:
        """16-char hex fingerprint of the public key."""
        self._ensure_loaded()
        if self._instance_id is None:
            raise RuntimeError(
                "internal: _ensure_loaded() returned without setting instance_id"
            )
        return self._instance_id

    def bind_engine(self, engine: WasmEngine) -> None:
        """Bind to a WasmEngine after init so sign() can delegate to it.

        Called automatically by ``wrap()``. The engine holds the private key
        after ``init()`` and handles Ed25519 signing in the WASM core.

        After binding, the Python-side private key material is zeroized. The
        WASM core retains its own copy in isolated linear memory. This limits
        the exposure window: a core dump or ``/proc/pid/mem`` read after this
        point cannot extract the key from the Python heap.
        """
        self._engine = engine
        # Zeroize Python-side key material now that WASM has its own copy.
        if self._private_key is not None:
            for i in range(len(self._private_key)):
                self._private_key[i] = 0
            self._private_key = None
            self._zeroized = True

    def sign(self, payload: bytes) -> bytes:
        """Sign a payload via the WASM core.

        Requires ``bind_engine()`` to have been called (happens automatically
        during ``wrap()``). Raises ``CheckrdInitError`` if not bound.
        """
        if self._engine is None:
            raise CheckrdInitError(
                "LocalIdentity not bound to engine. "
                "Call wrap() first, or use bind_engine() manually."
            )
        # WasmEngine.sign() -- avoid circular import by using duck typing
        return self._engine.sign(payload)


class ExternalIdentity:
    """Base for identity providers where the key lives outside the process.

    Subclass this for AWS KMS, GCP Cloud KMS, Azure Key Vault, or hardware
    security modules. The private key never leaves the secure enclave --
    ``private_key_bytes`` returns ``None`` and the WASM core runs in
    anonymous mode (no local signing).

    Subclasses must implement ``sign()``, ``public_key``, and ``instance_id``.

    .. note::

        AWS KMS does not natively support Ed25519 keys. The example below
        uses ECDSA_SHA_256 as a placeholder. For production Checkrd
        deployments, use a ``LocalIdentity`` (the default) or generate an
        Ed25519 key externally and load it via ``LocalIdentity.from_bytes()``.
        True KMS-managed Ed25519 support is tracked in Phase 2 of the
        identity architecture.

    Example KMS implementation (ECDSA — not Ed25519-compatible)::

        class AwsKmsIdentity(ExternalIdentity):
            \"\"\"Example only. ECDSA signatures are NOT compatible with
            Checkrd's Ed25519 verification. Use LocalIdentity for
            production deployments until KMS Ed25519 support ships.\"\"\"

            def __init__(self, key_id: str, region: str = "us-east-1"):
                import boto3
                self._client = boto3.client("kms", region_name=region)
                self._key_id = key_id
                pk_resp = self._client.get_public_key(KeyId=key_id)
                # NOTE: KMS returns DER-encoded public key, not raw 32 bytes.
                # You must extract the raw key from the SubjectPublicKeyInfo.
                self._public_key_bytes = pk_resp["PublicKey"]
                self._instance_id = self._public_key_bytes[:8].hex()

            @property
            def public_key(self) -> bytes:
                return self._public_key_bytes

            @property
            def instance_id(self) -> str:
                return self._instance_id

            def sign(self, payload: bytes) -> bytes:
                resp = self._client.sign(
                    KeyId=self._key_id,
                    Message=payload,
                    MessageType="RAW",
                    SigningAlgorithm="ECDSA_SHA_256",
                )
                return resp["Signature"]
    """

    @property
    def private_key_bytes(self) -> Optional[bytes]:
        """Always None -- the key never leaves the secure enclave."""
        return None

    @property
    def public_key(self) -> bytes:
        raise NotImplementedError

    @property
    def instance_id(self) -> str:
        raise NotImplementedError

    def sign(self, payload: bytes) -> bytes:
        raise NotImplementedError
