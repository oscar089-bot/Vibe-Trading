"""OS-backed secret storage for local trading connection instances."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from src.config.paths import get_runtime_root

SERVICE_NAME = "Vibe-Trading"
_INSTALL_HINT = (
    "OS credential vault support is not installed. Install the optional extra: "
    'pip install "vibe-trading-ai[keyring]" (or pip install keyring).'
)
# Raised when ``keyring`` is importable but resolved to its non-functional
# ``fail``/``null`` sentinel -- the state a headless server reaches when no
# Secret Service / Keychain / Credential Manager is reachable and no file
# fallback is installed. Names both cures so the operator never faces
# keyring's own "No recommended backend" text inside a web request.
_HEADLESS_HINT = (
    "No OS credential vault backend is available. On a headless server, "
    'install the file-based fallback: pip install "vibe-trading-ai[keyring]" '
    "(or pip install keyrings.alt); or pin a backend with PYTHON_KEYRING_BACKEND."
)
# Optional override for where the file fallback stores secrets. Defaults to
# ``<runtime root>/secrets`` so the Docker ``vibe-home`` volume persists it.
_KEYRING_DIR_ENV_VAR = "VIBE_TRADING_KEYRING_DIR"


class CredentialBackend(Protocol):
    """Minimal subset of the ``keyring`` API used by :class:`CredentialStore`."""

    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


def _keyring_dir() -> Path:
    """Return the durable directory for the file-based keyring fallback.

    Defaults to ``<runtime root>/secrets`` (``~/.vibe-trading/secrets`` unless
    ``VIBE_TRADING_HOME`` overrides the root) so a container's ``vibe-home``
    volume persists it across recreation. ``VIBE_TRADING_KEYRING_DIR`` overrides
    the location outright.
    """
    override = os.environ.get(_KEYRING_DIR_ENV_VAR, "").strip()
    root = Path(override).expanduser() if override else get_runtime_root() / "secrets"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _durable_file_path(backend: Any) -> Path:
    """Return the runtime-root path a file-backed keyring should use."""
    filename = Path(getattr(backend, "filename", "keyring_pass.cfg")).name
    return _keyring_dir() / filename


def _pin_if_file_backend(backend: Any) -> None:
    """Repoint a ``keyrings.alt`` file backend at the durable runtime root.

    The default location (``~/.local/share/python_keyring``) is neither
    persisted by the Docker volume nor writable under a read-only rootfs, so a
    file backend is always pinned to ``<runtime root>/secrets``.
    """
    if type(backend).__module__.split(".")[0] != "keyrings":
        return
    try:
        backend.file_path = str(_durable_file_path(backend))
    except Exception:
        # A non-settable path still leaves the backend usable for reads.
        pass


def _build_file_backend() -> Any | None:
    """Construct a non-interactive file backend, or return ``None`` if absent.

    Uses ``PlaintextKeyring`` deliberately: ``EncryptedKeyring`` carries a
    higher priority and prompts via ``getpass`` on first use, which would hang a
    headless server inside a request.
    """
    try:
        from keyrings.alt.file import PlaintextKeyring
    except ModuleNotFoundError:
        return None
    backend = PlaintextKeyring()
    backend.file_path = str(_durable_file_path(backend))
    return backend


def _resolve_backend(keyring_module: Any) -> Any:
    """Resolve a functional keyring backend, or raise an actionable error.

    Resolution order:
      1. ``PYTHON_KEYRING_BACKEND`` -- an explicit operator choice is honoured.
      2. A real OS vault (Keychain / Credential Manager / Secret Service) --
         used as-is.
      3. A ``keyrings.alt`` file backend -- installed with a durable path.
      4. Nothing usable -- raise :data:`_HEADLESS_HINT`.
    """
    from keyring.backends import fail

    non_functional: tuple[type, ...] = (fail.Keyring,)
    try:
        from keyring.backends import null

        non_functional = (fail.Keyring, null.Keyring)
    except Exception:  # pragma: no cover - null ships with keyring
        pass

    current = keyring_module.get_keyring()
    if os.environ.get("PYTHON_KEYRING_BACKEND", "").strip():
        return current
    if not isinstance(current, non_functional):
        _pin_if_file_backend(current)
        return current
    fallback = _build_file_backend()
    if fallback is None:
        raise RuntimeError(_HEADLESS_HINT)
    keyring_module.set_keyring(fallback)
    return fallback


class CredentialStore:
    """Store secrets in Keychain/Credential Manager/Secret Service via keyring.

    The ``keyring`` package is an optional extra: it is imported the first time
    a secret is actually read or written, so importing this module — and every
    module that only needs connection metadata — stays dependency-free.

    Args:
        backend: Optional backend implementing the ``keyring`` password API.
            Tests pass an in-memory double; production leaves it unset so the
            real OS vault is imported lazily.
    """

    def __init__(self, backend: CredentialBackend | None = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> CredentialBackend:
        """Return the credential backend, importing ``keyring`` on first use.

        Returns:
            The injected backend, otherwise the imported ``keyring`` module.

        Raises:
            RuntimeError: If ``keyring`` is not installed. The message names
                the optional extra that provides it.
        """
        if self._backend is None:
            try:
                import keyring
            except ModuleNotFoundError as exc:  # optional extra
                raise RuntimeError(_INSTALL_HINT) from exc

            self._backend = _resolve_backend(keyring)
        return self._backend

    @staticmethod
    def reference(connection_id: str) -> str:
        """Return the opaque vault reference stored beside a connection.

        Args:
            connection_id: Identifier of the local connection instance.

        Returns:
            A ``keyring://`` reference that names where the secret lives; it
            never contains the secret itself.
        """
        return f"keyring://vibe-trading/{connection_id}"

    @staticmethod
    def _username(connection_id: str, field: str) -> str:
        return f"connection:{connection_id}:{field}"

    def save(self, connection_id: str, values: Mapping[str, str]) -> None:
        """Persist non-empty values; blank fields leave an existing secret unchanged.

        Args:
            connection_id: Identifier of the local connection instance.
            values: Mapping of credential field name to secret value. Empty or
                whitespace-only values are skipped so a partial form submission
                cannot erase a stored secret.

        Raises:
            RuntimeError: If the ``keyring`` extra is not installed.
        """
        for field, raw_value in values.items():
            value = str(raw_value or "").strip()
            if value:
                self.backend.set_password(
                    SERVICE_NAME,
                    self._username(connection_id, field),
                    value,
                )

    def load(self, connection_id: str, fields: Iterable[str]) -> dict[str, str]:
        """Read the stored secrets for a connection.

        Args:
            connection_id: Identifier of the local connection instance.
            fields: Credential field names declared by the connector manifest.

        Returns:
            Mapping of field name to secret value, omitting fields that have
            no stored value.

        Raises:
            RuntimeError: If the ``keyring`` extra is not installed.
        """
        values: dict[str, str] = {}
        for field in fields:
            value = self.backend.get_password(
                SERVICE_NAME,
                self._username(connection_id, field),
            )
            if value:
                values[field] = value
        return values

    def status(self, connection_id: str, fields: Iterable[str]) -> dict[str, bool]:
        """Report which credential fields have a stored value.

        Args:
            connection_id: Identifier of the local connection instance.
            fields: Credential field names declared by the connector manifest.

        Returns:
            Mapping of field name to a boolean presence flag. A field whose
            backend lookup fails is reported as missing rather than raising, so
            a locked vault cannot break the connection listing.

        Raises:
            RuntimeError: If the ``keyring`` extra is not installed.
        """
        field_names = tuple(fields)
        try:
            backend = self.backend
        except RuntimeError:
            return {field: False for field in field_names}
        result = {}
        for field in field_names:
            try:
                result[field] = bool(
                    backend.get_password(
                        SERVICE_NAME,
                        self._username(connection_id, field),
                    )
                )
            except Exception:
                result[field] = False
        return result

    def delete(self, connection_id: str, fields: Iterable[str]) -> None:
        """Remove the stored secrets for a connection.

        Args:
            connection_id: Identifier of the local connection instance.
            fields: Credential field names declared by the connector manifest.

        Raises:
            RuntimeError: If the ``keyring`` extra is not installed.
            Exception: Whatever the backend raises for a failure other than a
                missing entry, which is treated as already deleted.
        """
        backend = self.backend
        for field in fields:
            try:
                backend.delete_password(
                    SERVICE_NAME,
                    self._username(connection_id, field),
                )
            except Exception as exc:  # keyring backends differ on missing entries
                if "not found" not in str(exc).lower():
                    raise
