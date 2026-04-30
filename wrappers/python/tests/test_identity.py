"""Tests for the identity provider interface and LocalIdentity."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from checkrd.exceptions import CheckrdInitError
from checkrd.identity import (
    ExternalIdentity,
    IdentityProvider,
    LocalIdentity,
    _KEY_FILE_SIZE,
)
from tests.conftest import requires_wasm


# ============================================================
# IdentityProvider protocol compliance
# ============================================================


class _StubKmsIdentity(ExternalIdentity):
    """Minimal KMS stub for protocol compliance testing."""

    def __init__(self) -> None:
        self._pk = b"\x01" * 32

    @property
    def public_key(self) -> bytes:
        return self._pk

    @property
    def instance_id(self) -> str:
        return self._pk[:8].hex()

    def sign(self, payload: bytes) -> bytes:
        return b"\xaa" * 64


class TestIdentityProviderProtocol:
    def test_local_identity_has_provider_interface(self) -> None:
        # Verify the class exposes all IdentityProvider attributes
        assert hasattr(LocalIdentity, "private_key_bytes")
        assert hasattr(LocalIdentity, "public_key")
        assert hasattr(LocalIdentity, "instance_id")
        assert hasattr(LocalIdentity, "sign")

    def test_external_identity_is_identity_provider(self) -> None:
        stub = _StubKmsIdentity()
        assert isinstance(stub, IdentityProvider)

    def test_external_private_key_is_none(self) -> None:
        stub = _StubKmsIdentity()
        assert stub.private_key_bytes is None

    def test_external_sign_returns_bytes(self) -> None:
        stub = _StubKmsIdentity()
        sig = stub.sign(b"payload")
        assert isinstance(sig, bytes)
        assert len(sig) == 64

    def test_external_instance_id(self) -> None:
        stub = _StubKmsIdentity()
        assert len(stub.instance_id) == 16
        assert stub.instance_id == ("01" * 8)


# ============================================================
# LocalIdentity -- key file management
# ============================================================


@requires_wasm
class TestLocalIdentityGeneration:
    def test_generates_key_on_first_use(self, tmp_path: Path) -> None:
        key_path = tmp_path / "identity.key"
        li = LocalIdentity(key_path=key_path)

        assert li.private_key_bytes is not None
        assert len(li.private_key_bytes) == 32
        assert len(li.public_key) == 32
        assert key_path.exists()
        assert len(key_path.read_bytes()) == _KEY_FILE_SIZE

    def test_key_file_permissions(self, tmp_path: Path) -> None:
        key_path = tmp_path / "identity.key"
        li = LocalIdentity(key_path=key_path)
        _ = li.private_key_bytes  # triggers key generation
        mode = key_path.stat().st_mode
        assert mode & stat.S_IRUSR  # owner read
        assert mode & stat.S_IWUSR  # owner write
        assert not (mode & stat.S_IRGRP)  # no group read
        assert not (mode & stat.S_IROTH)  # no other read

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        key_path = tmp_path / "deep" / "nested" / "identity.key"
        li = LocalIdentity(key_path=key_path)
        assert li.private_key_bytes is not None
        assert key_path.exists()

    def test_instance_id_is_hex(self, tmp_path: Path) -> None:
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        iid = li.instance_id
        assert len(iid) == 16
        assert all(c in "0123456789abcdef" for c in iid)

    def test_stable_across_reloads(self, tmp_path: Path) -> None:
        key_path = tmp_path / "identity.key"
        li1 = LocalIdentity(key_path=key_path)
        pk1 = li1.private_key_bytes
        iid1 = li1.instance_id

        li2 = LocalIdentity(key_path=key_path)
        assert li2.private_key_bytes == pk1
        assert li2.instance_id == iid1

    def test_different_keys_different_instance_ids(self, tmp_path: Path) -> None:
        li1 = LocalIdentity(key_path=tmp_path / "key1")
        li2 = LocalIdentity(key_path=tmp_path / "key2")
        assert li1.instance_id != li2.instance_id


@requires_wasm
class TestLocalIdentityLoading:
    def test_loads_existing_key(self, tmp_path: Path) -> None:
        key_path = tmp_path / "identity.key"
        # Generate first
        li1 = LocalIdentity(key_path=key_path)
        original_pk = li1.public_key

        # Load from file
        li2 = LocalIdentity(key_path=key_path)
        assert li2.public_key == original_pk

    def test_rejects_corrupt_key_file(self, tmp_path: Path) -> None:
        key_path = tmp_path / "identity.key"
        key_path.write_bytes(b"too short")

        li = LocalIdentity(key_path=key_path)
        with pytest.raises(CheckrdInitError, match="expected 64 bytes"):
            _ = li.private_key_bytes

    def test_rejects_truncated_key_file(self, tmp_path: Path) -> None:
        key_path = tmp_path / "identity.key"
        key_path.write_bytes(b"\x00" * 32)  # only private, missing public

        li = LocalIdentity(key_path=key_path)
        with pytest.raises(CheckrdInitError):
            _ = li.private_key_bytes

    def test_rejects_mismatched_public_key(self, tmp_path: Path) -> None:
        """Catch corruption where stored public key doesn't match private key."""
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        priv_a, _ = WasmEngine.generate_keypair()
        _, pub_b = WasmEngine.generate_keypair()
        # Write private key A with public key B
        key_path.write_bytes(priv_a + pub_b)

        li = LocalIdentity(key_path=key_path)
        with pytest.raises(CheckrdInitError, match="does not match"):
            _ = li.private_key_bytes

    def test_accepts_valid_key_file(self, tmp_path: Path) -> None:
        """Valid key file with matching private/public pair loads without error."""
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        priv, pub = WasmEngine.generate_keypair()
        key_path.write_bytes(priv + pub)

        li = LocalIdentity(key_path=key_path)
        assert li.private_key_bytes == priv
        assert li.public_key == pub


@requires_wasm
@pytest.mark.slow
@pytest.mark.xdist_group("serial")
class TestLocalIdentityConcurrency:
    """Verify O_CREAT|O_EXCL prevents the race condition."""

    def test_concurrent_generation_produces_one_key(self, tmp_path: Path) -> None:
        import threading

        key_path = tmp_path / "identity.key"
        results: list[bytes] = []
        errors: list[Exception] = []

        def load() -> None:
            try:
                li = LocalIdentity(key_path=key_path)
                results.append(li.public_key)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=load) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), f"thread {t.name} hung"

        assert not errors, f"unexpected errors: {errors}"
        # All threads must see the same public key (the winner's key).
        assert len(set(results)) == 1, "all threads must converge on one key"
        # File must be exactly 64 bytes.
        assert len(key_path.read_bytes()) == 64

    def test_file_permissions_after_race(self, tmp_path: Path) -> None:
        import threading

        key_path = tmp_path / "identity.key"

        def load() -> None:
            li = LocalIdentity(key_path=key_path)
            _ = li.private_key_bytes

        threads = [threading.Thread(target=load) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), f"thread {t.name} hung"

        mode = key_path.stat().st_mode
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)


_skip_if_root = pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses filesystem permission checks",
)
_skip_if_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix permission model not available on Windows",
)


@requires_wasm
@_skip_if_root
@_skip_if_windows
class TestLocalIdentityFileErrors:
    """Error paths for key file I/O."""

    def test_read_only_directory_prevents_generation(self, tmp_path: Path) -> None:
        """Key generation fails gracefully when the directory is read-only."""
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        key_path = ro_dir / "identity.key"
        ro_dir.chmod(0o444)

        try:
            li = LocalIdentity(key_path=key_path)
            with pytest.raises((CheckrdInitError, OSError, PermissionError)):
                _ = li.private_key_bytes
        finally:
            ro_dir.chmod(0o755)

    def test_unreadable_key_file_raises(self, tmp_path: Path) -> None:
        """Key file exists but is not readable."""
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        private, public = WasmEngine.generate_keypair()
        key_path.write_bytes(private + public)
        key_path.chmod(0o000)

        try:
            li = LocalIdentity(key_path=key_path)
            with pytest.raises((CheckrdInitError, PermissionError)):
                _ = li.private_key_bytes
        finally:
            key_path.chmod(0o644)

    def test_zero_byte_key_file_raises(self, tmp_path: Path) -> None:
        """Empty key file (0 bytes) is rejected."""
        key_path = tmp_path / "identity.key"
        key_path.write_bytes(b"")

        li = LocalIdentity(key_path=key_path)
        with pytest.raises(CheckrdInitError, match="expected 64 bytes"):
            _ = li.private_key_bytes

    def test_oversized_key_file_raises(self, tmp_path: Path) -> None:
        """Key file with more than 64 bytes is rejected."""
        key_path = tmp_path / "identity.key"
        key_path.write_bytes(b"\x00" * 128)

        li = LocalIdentity(key_path=key_path)
        with pytest.raises(CheckrdInitError, match="expected 64 bytes"):
            _ = li.private_key_bytes

    def test_from_file_unreadable_raises(self, tmp_path: Path) -> None:
        """from_file with unreadable file raises clear error."""
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        private, public = WasmEngine.generate_keypair()
        key_path.write_bytes(private + public)
        key_path.chmod(0o000)

        try:
            with pytest.raises((CheckrdInitError, PermissionError)):
                LocalIdentity.from_file(key_path)
        finally:
            key_path.chmod(0o644)


class TestLocalIdentityDefaults:
    def test_default_path_uses_home(self) -> None:
        li = LocalIdentity()
        assert ".checkrd" in str(li._key_path)
        assert li._key_path.name == "identity.key"

    def test_respects_config_dir_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        li = LocalIdentity()
        assert li._key_path == tmp_path / "identity.key"

    def test_sign_raises_before_bind(self) -> None:
        li = LocalIdentity()
        with pytest.raises(CheckrdInitError, match="not bound"):
            li.sign(b"payload")


# ============================================================
# ExternalIdentity base class
# ============================================================


@requires_wasm
class TestKeyZeroization:
    """Verify private key material is zeroized from Python memory after bind_engine().

    After the WASM engine has the key, the Python-side bytearray must be zeroed
    to limit the exposure window for core dumps and /proc/pid/mem reads.
    """

    def test_private_key_available_before_bind(self, tmp_path: Path) -> None:
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        pk = li.private_key_bytes
        assert pk is not None
        assert len(pk) == 32

    def test_private_key_none_after_bind(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        pk = li.private_key_bytes
        assert pk is not None

        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=pk,
        )
        li.bind_engine(engine)

        # After binding, the Python-side key is gone
        assert li.private_key_bytes is None

    def test_sign_still_works_after_zeroization(self, tmp_path: Path) -> None:
        """Signing delegates to the WASM engine, which has its own copy."""
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        pk = li.private_key_bytes
        assert pk is not None

        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=pk,
        )
        li.bind_engine(engine)

        # Key is zeroized in Python but signing still works via WASM
        assert li.private_key_bytes is None
        sig = li.sign(b"test payload")
        assert isinstance(sig, bytes)
        assert len(sig) == 64

    def test_public_key_survives_zeroization(self, tmp_path: Path) -> None:
        """Public key is NOT zeroized — it's not sensitive."""
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        pub = li.public_key
        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=li.private_key_bytes or b"",
        )
        li.bind_engine(engine)

        assert li.public_key == pub
        assert li.instance_id == pub[:8].hex()

    def test_internal_bytearray_is_zeroed(self, tmp_path: Path) -> None:
        """The actual bytearray object is filled with zeros before being dropped."""
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        _ = li.private_key_bytes

        # Grab a reference to the internal bytearray before bind_engine zeros it
        internal_key = li._private_key
        assert internal_key is not None
        assert any(b != 0 for b in internal_key), "key should have non-zero bytes"

        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=bytes(internal_key),
        )
        li.bind_engine(engine)

        # The original bytearray should now be all zeros
        assert all(b == 0 for b in internal_key), "bytearray should be zeroed in-place"


@requires_wasm
class TestPermissiveFileWarning:
    """Verify that loading an existing key file with too-open permissions logs a warning."""

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Unix permission model not available"
    )
    @pytest.mark.skipif(
        hasattr(os, "getuid") and os.getuid() == 0,
        reason="root bypasses permission checks"
    )
    def test_warns_on_world_readable_key(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        private, public = WasmEngine.generate_keypair()
        key_path.write_bytes(private + public)
        key_path.chmod(0o644)  # world-readable

        with caplog.at_level("WARNING", logger="checkrd"):
            li = LocalIdentity(key_path=key_path)
            _ = li.public_key

        assert any("permissions" in r.message and "0644" in r.message for r in caplog.records)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Unix permission model not available"
    )
    def test_no_warning_on_correct_permissions(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        private, public = WasmEngine.generate_keypair()
        key_path.write_bytes(private + public)
        key_path.chmod(0o600)

        with caplog.at_level("WARNING", logger="checkrd"):
            li = LocalIdentity(key_path=key_path)
            _ = li.public_key

        assert not any("permissions" in r.message for r in caplog.records)


class TestExternalIdentity:
    def test_private_key_always_none(self) -> None:
        ext = ExternalIdentity()
        assert ext.private_key_bytes is None

    def test_sign_not_implemented(self) -> None:
        ext = ExternalIdentity()
        with pytest.raises(NotImplementedError):
            ext.sign(b"payload")

    def test_public_key_not_implemented(self) -> None:
        ext = ExternalIdentity()
        with pytest.raises(NotImplementedError):
            _ = ext.public_key

    def test_instance_id_not_implemented(self) -> None:
        ext = ExternalIdentity()
        with pytest.raises(NotImplementedError):
            _ = ext.instance_id


# ============================================================
# WasmEngine.generate_keypair
# ============================================================


@requires_wasm
class TestLocalIdentitySign:
    """Tests that LocalIdentity.sign() delegates to the WASM engine."""

    def test_sign_returns_64_bytes(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        key_path = tmp_path / "identity.key"
        li = LocalIdentity(key_path=key_path)
        private_key = li.private_key_bytes
        assert private_key is not None

        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=private_key,
        )
        li.bind_engine(engine)

        sig = li.sign(b"hello world")
        assert isinstance(sig, bytes)
        assert len(sig) == 64

    def test_sign_matches_engine_directly(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=li.private_key_bytes or b"",
        )
        li.bind_engine(engine)

        payload = b"telemetry event payload"
        assert li.sign(payload) == engine.sign(payload)

    def test_sign_deterministic(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=li.private_key_bytes or b"",
        )
        li.bind_engine(engine)

        sig1 = li.sign(b"same message")
        sig2 = li.sign(b"same message")
        assert sig1 == sig2

    def test_different_messages_different_signatures(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        li = LocalIdentity(key_path=tmp_path / "identity.key")
        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=li.private_key_bytes or b"",
        )
        li.bind_engine(engine)

        assert li.sign(b"message A") != li.sign(b"message B")


@requires_wasm
class TestSignUniformProtocol:
    """Verify that sign() works uniformly across provider types."""

    def test_local_and_external_both_return_bytes(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        # Local provider
        li = LocalIdentity(key_path=tmp_path / "identity.key")
        engine = WasmEngine(
            '{"agent":"test","default":"allow","rules":[]}',
            "test-agent",
            private_key_bytes=li.private_key_bytes or b"",
        )
        li.bind_engine(engine)

        # External provider stub
        stub = _StubKmsIdentity()

        payload = b"test payload"
        local_sig = li.sign(payload)
        external_sig = stub.sign(payload)

        # Both return bytes -- protocol is uniform
        assert isinstance(local_sig, bytes)
        assert isinstance(external_sig, bytes)
        assert len(local_sig) == 64
        assert len(external_sig) == 64


@requires_wasm
class TestGenerateKeypair:
    def test_returns_32_byte_keys(self) -> None:
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        assert len(private) == 32
        assert len(public) == 32

    def test_unique_each_call(self) -> None:
        from checkrd.engine import WasmEngine

        priv_a, _ = WasmEngine.generate_keypair()
        priv_b, _ = WasmEngine.generate_keypair()
        assert priv_a != priv_b


# ============================================================
# Production constructors: from_env, from_file, from_bytes
# ============================================================
#
# These cover the production key provisioning workflow: customers generate
# a key once with `checkrd keygen`, distribute via secrets management,
# and load it via these classmethods. The default LocalIdentity() is the
# DEV-only fallback that auto-generates at ~/.checkrd/identity.key.


@requires_wasm
class TestLocalIdentityFromEnv:
    def test_loads_valid_base64_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from checkrd.engine import WasmEngine
        import base64

        private, public = WasmEngine.generate_keypair()
        monkeypatch.setenv("CHECKRD_AGENT_KEY", base64.b64encode(private).decode())

        identity = LocalIdentity.from_env()
        assert identity.private_key_bytes == private
        assert identity.public_key == public

    def test_uses_default_var_name_checkrd_agent_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd.engine import WasmEngine
        import base64

        private, _ = WasmEngine.generate_keypair()
        monkeypatch.setenv("CHECKRD_AGENT_KEY", base64.b64encode(private).decode())

        # No argument → reads CHECKRD_AGENT_KEY
        identity = LocalIdentity.from_env()
        assert identity.private_key_bytes == private

    def test_custom_var_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from checkrd.engine import WasmEngine
        import base64

        private, _ = WasmEngine.generate_keypair()
        monkeypatch.setenv("MY_CUSTOM_KEY", base64.b64encode(private).decode())

        identity = LocalIdentity.from_env("MY_CUSTOM_KEY")
        assert identity.private_key_bytes == private

    def test_missing_var_raises_with_helpful_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHECKRD_AGENT_KEY", raising=False)

        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_env()

        msg = str(exc_info.value)
        assert "CHECKRD_AGENT_KEY" in msg
        # Error should point users to the CLI
        assert "checkrd keygen" in msg

    def test_empty_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_AGENT_KEY", "")
        with pytest.raises(CheckrdInitError):
            LocalIdentity.from_env()

    def test_invalid_base64_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_AGENT_KEY", "this is not valid base64!")
        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_env()
        assert "base64" in str(exc_info.value).lower()

    def test_wrong_length_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Valid base64, but decodes to 16 bytes instead of 32.
        import base64

        too_short = base64.b64encode(b"\x00" * 16).decode()
        monkeypatch.setenv("CHECKRD_AGENT_KEY", too_short)
        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_env()
        assert "32 bytes" in str(exc_info.value)

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Common shell mistake: trailing newline from `$(checkrd keygen ...)`
        from checkrd.engine import WasmEngine
        import base64

        private, _ = WasmEngine.generate_keypair()
        encoded = base64.b64encode(private).decode()
        monkeypatch.setenv("CHECKRD_AGENT_KEY", f"  {encoded}\n")

        identity = LocalIdentity.from_env()
        assert identity.private_key_bytes == private

    def test_round_trip_with_keygen_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates: export CHECKRD_AGENT_KEY=$(checkrd keygen --private-only)
        from checkrd.engine import WasmEngine
        import base64

        private, public = WasmEngine.generate_keypair()
        monkeypatch.setenv(
            "CHECKRD_AGENT_KEY", base64.b64encode(private).decode()
        )
        identity = LocalIdentity.from_env()

        assert identity.private_key_bytes == private
        assert identity.public_key == public
        # Instance ID is deterministic from the public key
        assert identity.instance_id == public[:8].hex()


@requires_wasm
class TestLocalIdentityFromFile:
    def test_loads_existing_key_file(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        key_file = tmp_path / "identity.key"
        key_file.write_bytes(private + public)

        identity = LocalIdentity.from_file(key_file)
        assert identity.private_key_bytes == private
        assert identity.public_key == public

    def test_missing_file_raises_no_auto_generate(self, tmp_path: Path) -> None:
        # CRITICAL: from_file must NOT auto-generate, unlike the default
        # LocalIdentity() constructor. Production code must fail loudly.
        missing = tmp_path / "does-not-exist.key"
        assert not missing.exists()

        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_file(missing)

        # Critical assertion: the file is NOT created.
        assert not missing.exists(), \
            "from_file must not auto-create the file (unlike LocalIdentity())"

        # Error message should explain how to fix
        msg = str(exc_info.value)
        assert "not auto-generate" in msg or "does NOT auto-generate" in msg

    def test_wrong_size_raises(self, tmp_path: Path) -> None:
        key_file = tmp_path / "wrong-size.key"
        key_file.write_bytes(b"\x00" * 32)  # only 32 bytes, need 64

        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_file(key_file)
        assert "64 bytes" in str(exc_info.value)
        assert "got 32" in str(exc_info.value)

    def test_corrupt_key_raises(self, tmp_path: Path) -> None:
        # Public key in the file doesn't match the private key.
        from checkrd.engine import WasmEngine

        private_a, _ = WasmEngine.generate_keypair()
        _, public_b = WasmEngine.generate_keypair()  # mismatched

        key_file = tmp_path / "corrupt.key"
        key_file.write_bytes(private_a + public_b)

        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_file(key_file)
        assert "does not match" in str(exc_info.value).lower()

    def test_string_path_works(self, tmp_path: Path) -> None:
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        key_file = tmp_path / "identity.key"
        key_file.write_bytes(private + public)

        # Pass as string, not Path
        identity = LocalIdentity.from_file(str(key_file))
        assert identity.public_key == public

    def test_interop_with_default_constructor_file(self, tmp_path: Path) -> None:
        # Generate a dev key file via the default constructor, then load it
        # via from_file. The two paths must produce identical state.
        dev = LocalIdentity(key_path=tmp_path / "dev.key")
        dev_private = dev.private_key_bytes
        dev_public = dev.public_key

        prod = LocalIdentity.from_file(tmp_path / "dev.key")
        assert prod.private_key_bytes == dev_private
        assert prod.public_key == dev_public
        assert prod.instance_id == dev.instance_id


@requires_wasm
class TestLocalIdentityFromBytes:
    def test_loads_valid_32_byte_key(self) -> None:
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        identity = LocalIdentity.from_bytes(private)

        assert identity.private_key_bytes == private
        assert identity.public_key == public
        assert identity.instance_id == public[:8].hex()

    @pytest.mark.parametrize("size", [0, 1, 16, 31, 33, 64, 100])
    def test_wrong_size_raises(self, size: int) -> None:
        with pytest.raises(CheckrdInitError) as exc_info:
            LocalIdentity.from_bytes(b"\x00" * size)
        assert "32 bytes" in str(exc_info.value)
        assert f"got {size}" in str(exc_info.value)

    def test_non_bytes_raises(self) -> None:
        with pytest.raises(CheckrdInitError):
            LocalIdentity.from_bytes("not bytes")  # type: ignore[arg-type]

    def test_bytearray_accepted(self) -> None:
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        # bytearray should also work (subclass of bytes-like)
        identity = LocalIdentity.from_bytes(bytearray(private))
        assert identity.public_key == public

    def test_derives_public_key_correctly(self) -> None:
        from checkrd.engine import WasmEngine

        private, expected_public = WasmEngine.generate_keypair()
        identity = LocalIdentity.from_bytes(private)

        # Sanity: the derived public matches what generate_keypair returned
        assert identity.public_key == expected_public

    def test_instance_id_is_first_8_bytes_hex(self) -> None:
        from checkrd.engine import WasmEngine

        private, public = WasmEngine.generate_keypair()
        identity = LocalIdentity.from_bytes(private)
        assert identity.instance_id == public[:8].hex()
        assert len(identity.instance_id) == 16


@requires_wasm
class TestLocalIdentityConstructorParity:
    """All four constructors must produce identical state for the same key.

    This locks in the contract: how you load the key doesn't matter, the
    resulting LocalIdentity behaves the same.
    """

    def test_all_constructors_produce_same_public_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from checkrd.engine import WasmEngine
        import base64

        private, public = WasmEngine.generate_keypair()

        # Set up env var
        monkeypatch.setenv("CHECKRD_AGENT_KEY", base64.b64encode(private).decode())

        # Set up file
        key_file = tmp_path / "identity.key"
        key_file.write_bytes(private + public)

        # Construct via all three production paths
        from_env_id = LocalIdentity.from_env()
        from_file_id = LocalIdentity.from_file(key_file)
        from_bytes_id = LocalIdentity.from_bytes(private)

        # All four should agree on every observable field
        assert from_env_id.public_key == public
        assert from_file_id.public_key == public
        assert from_bytes_id.public_key == public

        assert from_env_id.private_key_bytes == private
        assert from_file_id.private_key_bytes == private
        assert from_bytes_id.private_key_bytes == private

        assert from_env_id.instance_id == from_file_id.instance_id
        assert from_file_id.instance_id == from_bytes_id.instance_id

    def test_default_constructor_still_auto_generates(self, tmp_path: Path) -> None:
        # Backwards compat: the existing dev workflow must still work.
        key_path = tmp_path / "dev.key"
        assert not key_path.exists()

        identity = LocalIdentity(key_path=key_path)
        # Force load
        _ = identity.public_key

        # Default constructor DOES auto-generate the file
        assert key_path.exists()
        assert key_path.stat().st_size == _KEY_FILE_SIZE

    def test_from_file_does_not_create_file_on_failure(self, tmp_path: Path) -> None:
        # Critical safety property: failed from_file leaves no trace.
        target = tmp_path / "should-not-exist.key"
        with pytest.raises(CheckrdInitError):
            LocalIdentity.from_file(target)
        assert not target.exists()


# ============================================================
# Invariant violations — verify the assert-replacement raises
# survive `python -O` (where `assert` is stripped).
# ============================================================


@requires_wasm
class TestLocalIdentityInvariants:
    """The Phase 1 audit replaced 5 ``assert`` statements with explicit
    ``raise`` clauses so they survive ``python -O``. These tests pin the
    behavior: forcing the impossible state must raise a typed error.
    """

    def test_public_key_raises_runtime_error_when_unloaded(
        self, tmp_path: Path
    ) -> None:
        identity = LocalIdentity(key_path=tmp_path / "dev.key")
        # Force the impossible state: _ensure_loaded ran but _public_key
        # was somehow not set. Pre-fix this raised AssertionError that
        # `python -O` would silently drop, leaking ``None``.
        identity._ensure_loaded()
        identity._public_key = None
        with pytest.raises(RuntimeError, match="_ensure_loaded"):
            _ = identity.public_key

    def test_instance_id_raises_runtime_error_when_unloaded(
        self, tmp_path: Path
    ) -> None:
        identity = LocalIdentity(key_path=tmp_path / "dev.key")
        identity._ensure_loaded()
        identity._instance_id = None
        with pytest.raises(RuntimeError, match="_ensure_loaded"):
            _ = identity.instance_id
