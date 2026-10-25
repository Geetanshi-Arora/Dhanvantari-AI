"""Read-only current-user DPAPI credentials and restricted runtime connection assembly."""

import ctypes
import json
import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path

from sqlalchemy import URL

PROJECT = "dgrmddokpylbjgpcqrem"
ROLE = "dhanvantari_runtime"
# Credential provenance is pinned to its reviewed preparation commit, not future HEADs.
CREDENTIAL_COMMIT = "7c47b25f59cfe6cc5606156b09375d56bcab7a6a"
ENTROPY = (PROJECT + ":" + ROLE).encode("ascii")
MAX_PAYLOAD = 65536
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
POINTER = ctypes.c_void_p


class _Blob(ctypes.Structure):
    _fields_ = [("size", DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _Attributes(ctypes.Structure):
    _fields_ = [("attributes", DWORD), ("tag", DWORD)]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate credential metadata")
        result[key] = value
    return result


def _decode_payload(data: bytearray) -> str:
    payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"project", "role", "commit", "password"}
        or payload["project"] != PROJECT
        or payload["role"] != ROLE
        or payload["commit"] != CREDENTIAL_COMMIT
        or not isinstance(payload["password"], str)
        or re.fullmatch(r"[A-Za-z0-9_-]{64}", payload["password"], re.ASCII) is None
    ):
        raise ValueError("Invalid runtime credential metadata")
    return payload["password"]


class _Windows:
    """Native APIs only; no shell, credential creation, or ACL modifications."""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Current-user DPAPI credentials require Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        self.crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        self._bind_api()

    @staticmethod
    def _bind(library, name, args, result):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result

    def _bind_api(self):
        self._bind(self.kernel, "GetCurrentProcess", [], HANDLE)
        self._bind(self.kernel, "CloseHandle", [HANDLE], BOOL)
        self._bind(self.kernel, "LocalFree", [POINTER], POINTER)
        self._bind(
            self.kernel,
            "CreateFileW",
            [ctypes.c_wchar_p, DWORD, DWORD, POINTER, DWORD, DWORD, HANDLE],
            HANDLE,
        )
        self._bind(
            self.kernel,
            "GetFileInformationByHandleEx",
            [HANDLE, ctypes.c_int, POINTER, DWORD],
            BOOL,
        )
        self._bind(self.kernel, "GetFileSizeEx", [HANDLE, ctypes.POINTER(ctypes.c_int64)], BOOL)
        self._bind(
            self.kernel, "ReadFile", [HANDLE, POINTER, DWORD, ctypes.POINTER(DWORD), POINTER], BOOL
        )
        self._bind(self.security, "OpenProcessToken", [HANDLE, DWORD, ctypes.POINTER(HANDLE)], BOOL)
        self._bind(
            self.security,
            "GetTokenInformation",
            [HANDLE, ctypes.c_int, POINTER, DWORD, ctypes.POINTER(DWORD)],
            BOOL,
        )
        self._bind(
            self.security,
            "ConvertSidToStringSidW",
            [POINTER, ctypes.POINTER(ctypes.c_wchar_p)],
            BOOL,
        )
        self._bind(
            self.security,
            "GetSecurityInfo",
            [
                HANDLE,
                ctypes.c_int,
                DWORD,
                ctypes.POINTER(POINTER),
                POINTER,
                ctypes.POINTER(POINTER),
                POINTER,
                ctypes.POINTER(POINTER),
            ],
            DWORD,
        )
        self._bind(
            self.security,
            "GetSecurityDescriptorControl",
            [POINTER, ctypes.POINTER(WORD), ctypes.POINTER(DWORD)],
            BOOL,
        )
        self._bind(self.security, "GetAce", [POINTER, DWORD, ctypes.POINTER(POINTER)], BOOL)
        self._bind(
            self.crypt,
            "CryptUnprotectData",
            [
                ctypes.POINTER(_Blob),
                POINTER,
                ctypes.POINTER(_Blob),
                POINTER,
                POINTER,
                DWORD,
                ctypes.POINTER(_Blob),
            ],
            BOOL,
        )

    def sid_text(self, pointer):
        text = ctypes.c_wchar_p()
        if not self.security.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
            raise OSError("Cannot inspect Windows identity")
        try:
            return text.value
        finally:
            self.kernel.LocalFree(ctypes.cast(text, POINTER))

    def current_sid(self):
        token = HANDLE()
        if not self.security.OpenProcessToken(
            self.kernel.GetCurrentProcess(),
            8,
            ctypes.byref(token),  # TOKEN_QUERY
        ):
            raise OSError("Cannot inspect Windows identity")
        try:
            size = DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 0 < size.value <= MAX_PAYLOAD:
                raise OSError("Invalid Windows identity information")
            buffer = ctypes.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(
                token,
                1,
                buffer,
                size.value,
                ctypes.byref(size),  # TokenUser
            ):
                raise OSError("Cannot inspect Windows identity")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES: its first member is a PSID.
            return self.sid_text(ctypes.cast(buffer, ctypes.POINTER(POINTER))[0])
        finally:
            self.kernel.CloseHandle(token)

    @contextmanager
    def open_path(self, path, *, directory, private, sid):
        # OPEN_EXISTING; OPEN_REPARSE_POINT + BACKUP_SEMANTICS. Share READ only.
        # Held ancestor handles prevent write/reparse/rename races during the read.
        access = (0x80 if directory else 0x80000000) | (0x20000 if private else 0)
        handle = self.kernel.CreateFileW(str(path), access, 1, None, 3, 0x02200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OSError("Cannot open protected credential path")
        try:
            info = _Attributes()
            if not self.kernel.GetFileInformationByHandleEx(
                handle,
                9,
                ctypes.byref(info),
                ctypes.sizeof(info),  # FileAttributeTagInfo
            ):
                raise OSError("Cannot inspect credential path")
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
                raise ValueError("Unexpected credential path type or reparse point")
            if private:
                self.check_acl(handle, sid, directory)
            yield handle
        finally:
            self.kernel.CloseHandle(handle)

    def check_acl(self, handle, sid, directory):
        owner, dacl, descriptor = POINTER(), POINTER(), POINTER()
        status = self.security.GetSecurityInfo(
            handle,
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),  # SE_FILE_OBJECT; OWNER | DACL_SECURITY_INFORMATION
        )
        try:
            if status or not descriptor or not owner or not dacl:
                raise OSError("Cannot inspect credential ownership and ACL")
            control, revision = WORD(), DWORD()
            if not self.security.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise OSError("Cannot inspect credential ACL")
            if self.sid_text(owner) != sid:
                raise ValueError("Credential ownership mismatch")
            if directory and not control.value & 0x1000:  # SE_DACL_PROTECTED
                raise ValueError("Credential directory inheritance must be disabled")
            # ACL header: BYTE revision, BYTE reserved, WORD size, WORD ACE count, WORD reserved.
            if WORD.from_address(dacl.value + 4).value != 1:
                raise ValueError("Unexpected credential access entries")
            ace = POINTER()
            if not self.security.GetAce(dacl, 0, ctypes.byref(ace)) or not ace:
                raise OSError("Cannot inspect credential access entry")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            size = WORD.from_address(ace.value + 2).value
            mask = DWORD.from_address(ace.value + 4).value
            if (
                kind != 0
                or size < 20  # ACCESS_ALLOWED_ACE with a SID
                or flags & ~0x13  # Only object/container inheritance and inherited flags allowed.
                or mask != 0x1F01FF  # FILE_ALL_ACCESS, for this user only.
                or self.sid_text(ace.value + 8) != sid
                or (directory and flags & 3 != 3)
            ):
                raise ValueError("Unexpected credential access entry")
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)

    def read(self, handle):
        size = ctypes.c_int64()
        if not self.kernel.GetFileSizeEx(handle, ctypes.byref(size)):
            raise OSError("Cannot inspect credential size")
        if not 0 < size.value <= MAX_PAYLOAD:
            raise ValueError("Invalid encrypted credential size")
        buffer = ctypes.create_string_buffer(size.value + 1)
        count = DWORD()
        if not self.kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise OSError("Cannot read protected credential")
        if count.value != size.value:
            raise ValueError("Encrypted credential changed during read")
        return buffer.raw[: count.value]

    def decrypt(self, ciphertext):
        if not 0 < len(ciphertext) <= MAX_PAYLOAD:
            raise ValueError("Invalid encrypted credential size")
        encoded = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
        entropy = (ctypes.c_ubyte * len(ENTROPY)).from_buffer_copy(ENTROPY)
        source, extra, result = _Blob(len(encoded), encoded), _Blob(len(entropy), entropy), _Blob()
        plaintext = bytearray()
        try:
            if not self.crypt.CryptUnprotectData(
                ctypes.byref(source),
                None,
                ctypes.byref(extra),
                None,
                None,
                1,
                ctypes.byref(result),  # CRYPTPROTECT_UI_FORBIDDEN, current-user scope.
            ):
                raise OSError("Cannot decrypt runtime credential for this Windows user")
            if not result.data or not 0 < result.size <= MAX_PAYLOAD:
                raise ValueError("Invalid decrypted credential size")
            plaintext = bytearray(result.size)
            ctypes.memmove(
                (ctypes.c_ubyte * result.size).from_buffer(plaintext), result.data, result.size
            )
            return _decode_payload(plaintext)
        finally:
            plaintext[:] = b"\0" * len(plaintext)
            if result.data:
                try:
                    ctypes.memset(result.data, 0, result.size)
                finally:
                    self.kernel.LocalFree(ctypes.cast(result.data, POINTER))


def _read_password(path: Path) -> str:
    try:
        if not path.is_absolute():
            raise ValueError("An absolute credential path is required")
        if path.resolve().is_relative_to(Path(__file__).resolve().parents[3]):
            raise ValueError("Runtime credentials must remain outside the checkout")
        native = _Windows()
        sid = native.current_sid()
        with ExitStack() as stack:
            for directory in reversed(path.parents):
                stack.enter_context(
                    native.open_path(
                        directory, directory=True, private=directory == path.parent, sid=sid
                    )
                )
            handle = stack.enter_context(
                native.open_path(path, directory=False, private=True, sid=sid)
            )
            return native.decrypt(native.read(handle))
    except Exception:
        raise RuntimeError(
            "Runtime credential loading failed; check private Windows settings"
        ) from None


def runtime_database_url(settings) -> URL | str:
    """The direct URL branch supports explicitly injected disposable test settings only."""
    if not settings.runtime_credential_file:
        return settings.database_url.get_secret_value()
    try:
        certificate = Path(settings.runtime_ca_file)
        credential = Path(settings.runtime_credential_file)
        if not certificate.is_absolute() or not certificate.is_file():
            raise ValueError("Missing runtime TLS certificate")
        if not credential.is_absolute():
            raise ValueError("An absolute runtime credential path is required")
        if credential.resolve().is_relative_to(Path(__file__).resolve().parents[3]):
            raise ValueError("Runtime credential must remain outside the checkout")
        password = _read_password(credential)
        if not isinstance(password, str) or not password:
            raise ValueError("Missing runtime password")
        return URL.create(
            "postgresql+psycopg",
            username=ROLE + "." + PROJECT,
            password=password,
            host=settings.runtime_pooler_host,
            port=5432,
            database="postgres",
            query={"sslmode": "verify-full", "sslrootcert": str(certificate)},
        )
    except Exception:
        raise RuntimeError(
            "Runtime credential loading unavailable; check private setup status"
        ) from None
