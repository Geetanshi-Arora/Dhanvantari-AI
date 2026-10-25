"""Fictional data and native API doubles only; never open a real DPAPI credential."""

import ctypes
import json
import traceback
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import runtime_credentials as credentials

PASSWORD = "Fictional_" + "a" * 54
SID = "S-1-5-21-FICTIONAL"


def payload(**overrides):
    return bytearray(
        json.dumps(
            {
                "project": credentials.PROJECT,
                "role": credentials.ROLE,
                "commit": credentials.CREDENTIAL_COMMIT,
                "password": PASSWORD,
                **overrides,
            }
        ).encode("utf-8")
    )


def native():
    return object.__new__(credentials._Windows)


def decryptor(data, success=True):
    api = native()
    output = ctypes.create_string_buffer(bytes(data), max(1, len(data)))
    freed = []

    def unprotect(source, description, entropy, reserved, prompt, flags, result):
        assert ctypes.string_at(source._obj.data, source._obj.size) == b"fictional ciphertext"
        assert ctypes.string_at(entropy._obj.data, entropy._obj.size) == (
            credentials.PROJECT + ":" + credentials.ROLE
        ).encode("ascii")
        assert flags == 1 and description is None and reserved is None and prompt is None
        result._obj.size = len(data)
        result._obj.data = ctypes.cast(output, ctypes.POINTER(ctypes.c_ubyte))
        return success

    def free(pointer):
        assert pointer.value == ctypes.addressof(output)
        assert output.raw == b"\0" * len(output.raw)
        freed.append(pointer.value)

    api.crypt = SimpleNamespace(CryptUnprotectData=unprotect)
    api.kernel = SimpleNamespace(LocalFree=free)
    return api, freed, output


def test_valid_decoding_uses_exact_format_and_clears_native_memory():
    api, freed, _ = decryptor(payload())
    assert api.decrypt(b"fictional ciphertext") == PASSWORD
    assert len(freed) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"project": "another-project"},
        {"role": "postgres"},
        {"commit": "wrong-commit"},
        {"password": "a" * 63},
        {"password": "a" * 65},
        {"password": "é" * 64},
        {"password": "+" * 64},
        {"password": "a" * 63 + "\n"},
        {"password": None},
        {"unexpected": "field"},
    ],
)
def test_metadata_and_password_validation(changes):
    api, freed, _ = decryptor(payload(**changes))
    with pytest.raises(ValueError):
        api.decrypt(b"fictional ciphertext")
    assert len(freed) == 1


@pytest.mark.parametrize(
    "data",
    [
        b"\xff",
        b"not JSON",
        b"[]",
        b"null",
        b"{}",
        b'{"password":"first","password":"second"}',
    ],
)
def test_corrupted_payload_clears_native_memory(data):
    api, freed, _ = decryptor(data)
    with pytest.raises(ValueError):
        api.decrypt(b"fictional ciphertext")
    assert len(freed) == 1


def test_wrong_user_or_dpapi_failure_clears_any_allocated_output():
    api, freed, _ = decryptor(payload(), success=False)
    with pytest.raises(OSError):
        api.decrypt(b"fictional ciphertext")
    assert len(freed) == 1


@pytest.mark.parametrize("data", [b"", b"x" * 65537])
def test_invalid_ciphertext_size_does_not_call_dpapi(data):
    api = native()
    api.crypt = SimpleNamespace(CryptUnprotectData=Mock())
    with pytest.raises(ValueError):
        api.decrypt(data)
    api.crypt.CryptUnprotectData.assert_not_called()


def test_native_types_and_bindings_preserve_pointer_width():
    assert ctypes.sizeof(credentials.DWORD) == 4
    assert credentials._Blob.data.offset == (8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4)
    api = native()

    class Library:
        def __getattr__(self, name):
            value = Mock()
            setattr(self, name, value)
            return value

    api.kernel, api.security, api.crypt = Library(), Library(), Library()
    api._bind_api()
    assert api.kernel.CreateFileW.restype is ctypes.c_void_p
    assert api.kernel.LocalFree.argtypes == [ctypes.c_void_p]
    assert api.crypt.CryptUnprotectData.argtypes[0] == ctypes.POINTER(credentials._Blob)
    assert api.crypt.CryptUnprotectData.argtypes[-1] == ctypes.POINTER(credentials._Blob)


def acl_api(
    *, owner=SID, trustee=SID, count=1, protected=True, mask=0x1F01FF, kind=0, flags=3, fail=False
):
    api = native()
    header = ctypes.create_string_buffer(8)
    credentials.WORD.from_buffer(header, 4).value = count
    ace = ctypes.create_string_buffer(32)
    ctypes.c_ubyte.from_buffer(ace, 0).value = kind
    ctypes.c_ubyte.from_buffer(ace, 1).value = flags
    credentials.WORD.from_buffer(ace, 2).value = 32
    credentials.DWORD.from_buffer(ace, 4).value = mask
    owner_address = ctypes.addressof(header) + 7
    descriptor_address = ctypes.addressof(header)
    freed = []

    def security(handle, object_type, info, owner_out, group, dacl, sacl, descriptor):
        assert object_type == 1 and info == 5 and group is None and sacl is None
        owner_out._obj.value = owner_address
        dacl._obj.value = ctypes.addressof(header)
        descriptor._obj.value = descriptor_address
        return 5 if fail else 0

    def control(descriptor, value, revision):
        value._obj.value = 0x1000 if protected else 0
        return True

    def get_ace(dacl, index, result):
        assert index == 0
        result._obj.value = ctypes.addressof(ace)
        return True

    api.security = SimpleNamespace(
        GetSecurityInfo=security, GetSecurityDescriptorControl=control, GetAce=get_ace
    )
    # sid_text receives both a c_void_p owner and a numeric ACE SID address.
    api.sid_text = lambda pointer: (
        owner
        if (pointer.value if hasattr(pointer, "value") else pointer) == owner_address
        else trustee
    )
    api.kernel = SimpleNamespace(LocalFree=lambda value: freed.append(value.value))
    api._test_buffers = (header, ace)
    return api, freed


@pytest.mark.parametrize("directory,flags", [(True, 3), (False, 0x10), (False, 0)])
def test_safe_current_user_only_acl(directory, flags):
    api, freed = acl_api(flags=flags)
    api.check_acl(123, SID, directory)
    assert len(freed) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "other"},
        {"trustee": "other"},
        {"count": 0},
        {"count": 2},
        {"protected": False},
        {"mask": 0x120089},
        {"kind": 1},
        {"flags": 0x0B},
        {"flags": 0x43},
        {"flags": 0},
        {"fail": True},
    ],
)
def test_unsafe_acls_are_rejected_and_descriptors_freed(changes):
    api, freed = acl_api(**changes)
    with pytest.raises((ValueError, OSError)):
        api.check_acl(123, SID, True)
    assert len(freed) == 1


def test_reparse_point_rejected_before_acl_or_content_read(tmp_path):
    api = native()
    closed = []

    def information(handle, kind, output, size):
        output._obj.attributes = 0x400
        return True

    def open_file(path, access, sharing, security, disposition, flags, template):
        assert sharing == 1 and disposition == 3 and flags == 0x02200000
        return 123

    api.kernel = SimpleNamespace(
        CreateFileW=open_file,
        GetFileInformationByHandleEx=information,
        CloseHandle=lambda value: closed.append(value),
    )
    api.check_acl = Mock()
    with pytest.raises(ValueError):
        with api.open_path(tmp_path / "runtime.dpapi", directory=False, private=True, sid=SID):
            pytest.fail("Reparse point must never be yielded")
    api.check_acl.assert_not_called()
    assert closed == [123]


@pytest.mark.parametrize("size", [0, 65537])
def test_encrypted_size_checked_before_read(size):
    api = native()

    def length(handle, output):
        output._obj.value = size
        return True

    api.kernel = SimpleNamespace(GetFileSizeEx=length, ReadFile=Mock())
    with pytest.raises(ValueError):
        api.read(123)
    api.kernel.ReadFile.assert_not_called()


def test_maximum_encrypted_size_is_accepted():
    api = native()

    def length(handle, output):
        output._obj.value = 65536
        return True

    def read(handle, buffer, size, count, overlapped):
        assert size == 65537
        ctypes.memset(buffer, ord("x"), 65536)
        count._obj.value = 65536
        return True

    api.kernel = SimpleNamespace(GetFileSizeEx=length, ReadFile=read)
    assert api.read(123) == b"x" * 65536


def test_credential_read_keeps_checked_ancestors_open(tmp_path, monkeypatch):
    path = tmp_path / "private" / "runtime.dpapi"
    api = SimpleNamespace(current_sid=lambda: SID)
    opened, closed = [], []

    @contextmanager
    def open_path(component, **options):
        assert options["sid"] == SID
        assert options["private"] == (component in (path.parent, path))
        opened.append(component)
        try:
            yield len(opened)
        finally:
            closed.append(component)

    def read(handle):
        assert opened == [*reversed(path.parents), path]
        assert closed == []
        return b"fictional ciphertext"

    api.open_path, api.read = open_path, read
    api.decrypt = lambda value: PASSWORD
    monkeypatch.setattr(credentials, "_Windows", lambda: api)
    assert credentials._read_password(path) == PASSWORD
    assert closed == list(reversed(opened))


def test_acl_failure_never_reads_or_decrypts(tmp_path, monkeypatch):
    api = SimpleNamespace(current_sid=lambda: SID, read=Mock(), decrypt=Mock())

    @contextmanager
    def reject(path, **kwargs):
        raise ValueError("FICTIONAL_PRIVATE_DIAGNOSTIC")
        yield

    api.open_path = reject
    monkeypatch.setattr(credentials, "_Windows", lambda: api)
    with pytest.raises(RuntimeError) as caught:
        credentials._read_password(tmp_path / "runtime.dpapi")
    assert "FICTIONAL_PRIVATE_DIAGNOSTIC" not in "".join(traceback.format_exception(caught.value))
    api.read.assert_not_called()
    api.decrypt.assert_not_called()


@pytest.mark.parametrize(
    "data,success",
    [
        (payload(role="FICTIONAL-PRIVATE-METADATA"), True),
        (b'{"private":"FICTIONAL-PRIVATE-METADATA"', True),
        (payload(), False),
    ],
)
def test_all_decode_failures_are_sanitized_at_reader_boundary(
    tmp_path, monkeypatch, capsys, data, success
):
    api, freed, _ = decryptor(data, success=success)
    api.current_sid = lambda: SID

    @contextmanager
    def open_path(path, **kwargs):
        yield 123

    api.open_path = open_path
    api.read = lambda handle: b"fictional ciphertext"
    monkeypatch.setattr(credentials, "_Windows", lambda: api)
    with pytest.raises(RuntimeError) as caught:
        credentials._read_password(tmp_path / "runtime.dpapi")
    output = capsys.readouterr()
    visible = output.out + output.err + "".join(traceback.format_exception(caught.value))
    assert PASSWORD not in visible
    assert "FICTIONAL-PRIVATE-METADATA" not in visible
    assert len(freed) == 1


def test_decode_failure_closes_all_ancestor_handles(tmp_path, monkeypatch):
    opened, closed = [], []

    @contextmanager
    def open_path(path, **kwargs):
        opened.append(path)
        try:
            yield 123
        finally:
            closed.append(path)

    def fail(data):
        raise ValueError("Fictional decode failure")

    api = SimpleNamespace(
        current_sid=lambda: SID,
        open_path=open_path,
        read=lambda handle: b"fictional ciphertext",
        decrypt=fail,
    )
    monkeypatch.setattr(credentials, "_Windows", lambda: api)
    with pytest.raises(RuntimeError):
        credentials._read_password(tmp_path / "runtime.dpapi")
    assert closed == list(reversed(opened))


def test_oversized_decrypted_payload_is_freed():
    api, freed, _ = decryptor(b"x" * 65537)
    with pytest.raises(ValueError, match="decrypted credential size"):
        api.decrypt(b"fictional ciphertext")
    assert len(freed) == 1


def test_short_read_is_rejected():
    api = native()

    def size(handle, output):
        output._obj.value = 10
        return True

    def read(handle, buffer, length, count, overlapped):
        count._obj.value = 9
        return True

    api.kernel = SimpleNamespace(GetFileSizeEx=size, ReadFile=read)
    with pytest.raises(ValueError, match="changed during read"):
        api.read(123)


def test_token_handle_is_closed_and_sid_pointer_is_not_truncated():
    api = native()
    sid = ctypes.create_string_buffer(b"fictional SID bytes")
    sid_address = ctypes.addressof(sid)
    token_value = 0x12345678
    closed = []

    def open_token(process, access, output):
        output._obj.value = token_value
        return True

    def information(token, kind, buffer, size, actual):
        actual._obj.value = ctypes.sizeof(ctypes.c_void_p) * 2
        if buffer is None:
            return False
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = sid_address
        return True

    api.kernel = SimpleNamespace(
        GetCurrentProcess=lambda: -1, CloseHandle=lambda handle: closed.append(handle.value)
    )
    api.security = SimpleNamespace(OpenProcessToken=open_token, GetTokenInformation=information)
    seen = []
    api.sid_text = lambda pointer: seen.append(pointer) or SID
    assert api.current_sid() == SID
    assert seen == [sid_address]
    assert closed == [token_value]
