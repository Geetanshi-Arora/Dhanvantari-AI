"""Real cookie/CSRF dependencies; mocked DB, Auth, audit and voice providers only.

Reuses existing offline auth fixture and fictional provider. No SQL, DB fixtures,
provider HTTP transports or application lifespan/server execution is permitted.
"""

import base64
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from test_patient_routes import NURSE, headers
from test_patient_routes import web as web
from test_voice_service import AUDIO, CAPS, CORRECTED, MEDIA, TEXT, Rig

from app.core.http import VoiceBodyLimit
from app.core.security import Principal
from app.routes.voice import VoiceRouteBindings, VoiceWorkflows
from app.schemas.voice import GNANI_CAPABILITIES
from app.services.voice import VoiceServiceError


@pytest.fixture
def voice(web, monkeypatch):
    # TestClient uses its in-process ASGI transport; all actual IP/DNS is forbidden.
    def denied(*args, **kwargs):
        raise AssertionError("Network forbidden in offline voice tests")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    rig = Rig()
    rig.principal = Principal(NURSE, "Fictional Nurse", "nurse", web.session.token_hash)
    rig.change(actor_id=NURSE, session_hash=web.session.token_hash)
    handles = {}
    rig.auth_count = 0
    rig.tts_approved = False
    rig.forced_context = {}
    patient_id, visit_id = rig.request.patient_id, rig.request.visit_id

    def authorize(*, principal, context, operation, text_digest):
        rig.auth_count += 1
        assert web.provider.user.called
        assert principal.id == NURSE and principal.session_hash == web.session.token_hash
        result = rig.decision.model_copy(
            update=dict(
                request_id=context.request_id,
                patient_id=context.patient_id,
                visit_id=context.visit_id,
                language=context.language,
            )
            | rig.forced_context
        )
        if context.patient_id != patient_id or context.visit_id != visit_id:
            result = result.model_copy(update={"patient_assigned": False})
        if operation == "tts" and rig.tts_approved:
            result = result.model_copy(
                update=dict(
                    synthesis_permitted=True,
                    tts_reviewed_by=NURSE,
                    tts_text_digest=sha256(CORRECTED.encode()).hexdigest(),
                )
            )
        return result

    def remember(handle):
        key = handle.transcript.draft.request_id
        if len(handles) >= 4 or key in handles:
            raise VoiceServiceError("capacity")
        handles[key] = handle

    def resolve(key):
        return handles.get(key)

    def update(old, new):
        key = old.transcript.draft.request_id
        if handles.get(key) is not old:
            raise VoiceServiceError("stale")
        handles[key] = new

    rig.service._authorize_callback = authorize
    web.app.state.voice_bindings = VoiceRouteBindings(
        enabled=True,
        single_process=True,
        service=rig.service,
        capabilities=CAPS,
        workflows=VoiceWorkflows(remember, resolve, update),
    )
    web.rig, web.handles = rig, handles
    web.root = f"/api/v1/voice/patients/{patient_id}/visits/{visit_id}"
    try:
        yield web
    finally:
        handles.clear()
        rig.service.close()


def transcription(**changes):
    return (
        dict(
            language="hi",
            media_type=MEDIA,
            byte_count=len(AUDIO),
            duration_ms=1000,
            filename="fictional.wav",
            audio_base64=base64.b64encode(AUDIO).decode(),
        )
        | changes
    )


def synthesis(**changes):
    return dict(language="hi", media_type=MEDIA, text=CORRECTED) | changes


def post(voice, path="/transcriptions", payload=None, **kwargs):
    return voice.client.post(
        voice.root + path,
        json=transcription() if payload is None else payload,
        headers=headers(),
        **kwargs,
    )


def get_draft(voice):
    response = post(voice)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["state"] == "unverified" and draft["original_text"] == TEXT
    return draft


def review(voice, draft, **changes):
    return post(
        voice,
        f"/transcripts/{draft['request_id']}/review",
        dict(
            language="hi",
            expected_revision=0,
            action="correct",
            corrected_text=CORRECTED,
        )
        | changes,
    )


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/transcriptions", transcription()),
        ("/synthesis", synthesis()),
        (
            f"/transcripts/{uuid4()}/review",
            dict(language="hi", expected_revision=0, action="confirm"),
        ),
    ],
)
def test_all_routes_disabled_at_startup(web, path, payload):
    assert web.app.state.voice_bindings == VoiceRouteBindings()
    response = web.client.post(
        "/api/v1/voice/patients/" + str(uuid4()) + "/visits/" + str(uuid4()) + path,
        json=payload,
        headers=headers(),
    )
    assert response.status_code == 503 and response.json()["detail"] == "Voice workflow unavailable"


def test_routes_registered_before_static_mount(voice):
    # A typed JSON draft proves the catch-all StaticFiles mount did not swallow it.
    draft = get_draft(voice)
    assert draft["patient_id"] == str(voice.rig.request.patient_id)
    assert len(voice.handles) == 1 and voice.rig.provider.calls == 1


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"enabled": "true"},
        {"single_process": False},
        {"single_process": "true"},
        {"service": None},
        {"workflows": None},
        {"capabilities": None},
    ],
)
def test_gate_requires_all_reviewed_server_bindings(voice, change):
    voice.app.state.voice_bindings = replace(voice.app.state.voice_bindings, **change)
    assert post(voice).status_code == 503
    assert voice.rig.provider.calls == 0


@pytest.mark.parametrize("mode", ["missing", "unknown", "expired", "corrupt", "mismatch"])
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/transcriptions", transcription()),
        ("/synthesis", synthesis()),
        (
            f"/transcripts/{uuid4()}/review",
            dict(language="hi", expected_revision=0, action="confirm"),
        ),
    ],
)
def test_invalid_sessions_all_routes(voice, mode, path, payload):
    if mode == "missing":
        voice.client.cookies.clear()
    elif mode == "unknown":
        voice.db.get.return_value = None
    elif mode == "expired":
        voice.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif mode == "corrupt":
        voice.session.encrypted_access_token = "fictional-corrupt-token"
    else:
        voice.provider.user.return_value = uuid4()
    assert post(voice, path, payload).status_code == 401
    assert voice.rig.provider.calls == 0 and voice.rig.auth_count == 0


@pytest.mark.parametrize("mode", ["inactive", "doctor"])
def test_active_nurse_database_profile_required(voice, mode):
    if mode == "inactive":
        voice.staff.active = False
    else:
        voice.staff.role = "doctor"
    assert post(voice).status_code == 403
    assert voice.rig.provider.calls == 0 and voice.rig.auth_count == 0


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/transcriptions", transcription()),
        ("/synthesis", synthesis()),
        (
            f"/transcripts/{uuid4()}/review",
            dict(language="hi", expected_revision=0, action="confirm"),
        ),
    ],
)
@pytest.mark.parametrize(
    "change",
    [
        {"Origin": None},
        {"Origin": "https://untrusted.example.com"},
        {"X-CSRF-Token": None},
        {"X-CSRF-Token": "wrong-token"},
    ],
)
def test_origin_and_csrf_before_voice_processing(voice, path, payload, change):
    request_headers = headers() | change
    request_headers = {key: value for key, value in request_headers.items() if value is not None}
    response = voice.client.post(voice.root + path, json=payload, headers=request_headers)
    assert response.status_code == 403
    assert voice.rig.provider.calls == 0 and voice.rig.auth_count == 0


@pytest.mark.parametrize(
    "field",
    [
        "authenticated",
        "active_nurse",
        "patient_assigned",
        "care_granted",
        "voice_notice_approved",
        "recording_permitted",
        "external_voice_processing_permitted",
    ],
)
def test_separate_permissions_fail_closed_before_provider(voice, field):
    voice.rig.change(**{field: False})
    assert post(voice).status_code == 403
    assert voice.rig.provider.calls == 0


def test_no_voice_specific_consent_adapter(voice):
    voice.rig.service._authorize_callback = None
    assert post(voice).status_code == 503
    assert voice.rig.provider.calls == 0


def test_unassigned_patient_or_visit(voice):
    voice.root = f"/api/v1/voice/patients/{uuid4()}/visits/{uuid4()}"
    assert post(voice).status_code == 403
    assert voice.rig.provider.calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"nurse_id": str(uuid4())},
        {"care": True},
        {"uploads": True},
        {"future_ai": True},
        {"request_id": str(uuid4())},
        {"filename": "../fictional.wav"},
        {"language": "en"},
        {"language": "hi-IN"},
        {"byte_count": 0},
        {"duration_ms": 60001},
        {"duration_ms": True},
        {"audio_base64": "*invalid*"},
        {"audio_base64": "YQ=="},
        {"audio_base64": "YR=="},
    ],
)
def test_strict_metadata_audio_and_no_client_authority(voice, changes):
    response = post(voice, payload=transcription(**changes))
    assert response.status_code == 422
    assert TEXT not in response.text and voice.rig.provider.calls == 0


@pytest.mark.parametrize(
    "payload", [b"{", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff", b'{"PRIVATE_SECRET": 1}']
)
def test_malformed_json_errors_sanitized(voice, payload):
    response = voice.client.post(
        voice.root + "/transcriptions",
        content=payload,
        headers=headers() | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "PRIVATE_SECRET" not in response.text


@pytest.mark.parametrize(
    "request_headers",
    [
        {"Content-Type": "audio/wav"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ],
)
def test_unsupported_encoding(voice, request_headers):
    response = voice.client.post(
        voice.root + "/transcriptions", content=b"{}", headers=headers() | request_headers
    )
    assert response.status_code == 415


@pytest.mark.parametrize(
    "path,limit", [("/transcriptions", VoiceBodyLimit.audio_json_limit), ("/synthesis", 32768)]
)
@pytest.mark.parametrize("chunked", [False, True])
def test_body_limits_actual_bytes_including_chunked(voice, path, limit, chunked):
    data = b"x" * (limit + 1)
    content = iter([data[:100], data[100:]]) if chunked else data
    response = voice.client.post(
        voice.root + path, content=content, headers=headers() | {"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert voice.rig.provider.calls == 0


def test_decoded_audio_limit_independent_of_json_ceiling(voice):
    audio = b"x" * (4 * 1024 * 1024 + 1)
    response = post(
        voice,
        payload=transcription(audio_base64=base64.b64encode(audio).decode(), byte_count=len(audio)),
    )
    assert response.status_code == 422 and voice.rig.provider.calls == 0


def test_unverified_draft_response_and_ephemeral_state_only(voice):
    draft = get_draft(voice)
    assert voice.rig.provider.calls == 1
    assert list(voice.handles) == [voice.rig.events[0]["request_id"]]
    assert len(voice.handles) == 1 and draft["request_id"] == str(next(iter(voice.handles)))
    assert voice.events == ["actor"]  # Existing JWT verification before actor lookup.
    assert voice.client.get("/api/v1/health/live").status_code == 200


def test_explicit_correction_then_confirmation_and_stale_revision(voice):
    draft = get_draft(voice)
    assert review(voice, draft, action="confirm", corrected_text=None).status_code == 409
    corrected = review(voice, draft)
    assert corrected.status_code == 200 and corrected.json()["state"] == "nurse_corrected"
    assert corrected.json()["draft"]["original_text"] == TEXT
    assert review(voice, draft).status_code == 409
    confirmed = review(voice, draft, expected_revision=1, action="confirm", corrected_text=None)
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "nurse_confirmed"
    assert len(confirmed.json()["reviews"]) == 2
    assert (
        review(voice, draft, expected_revision=2, action="confirm", corrected_text=None).status_code
        == 409
    )


@pytest.mark.parametrize(
    "mode", ["expired_handle", "patient", "visit", "language", "session", "consent_revision"]
)
def test_review_rejects_expired_and_changed_context(voice, mode):
    draft = get_draft(voice)
    kwargs = {}
    if mode == "expired_handle":
        voice.handles.clear()
    elif mode == "patient":
        voice.root = voice.root.replace(draft["patient_id"], str(uuid4()))
    elif mode == "visit":
        voice.root = voice.root.replace(draft["visit_id"], str(uuid4()))
    elif mode == "language":
        kwargs["language"] = "en"
    elif mode == "session":
        voice.session.token_hash = "b" * 64
    else:
        voice.rig.change(voice_revision=uuid4())
    assert review(voice, draft, **kwargs).status_code == 409


@pytest.mark.parametrize(
    "field", ["care_granted", "recording_permitted", "external_voice_processing_permitted"]
)
def test_inflight_revocation_discards_transcript(voice, field):
    voice.rig.provider.hook = lambda: voice.rig.change(**{field: False})
    response = post(voice)
    assert response.status_code == 409
    assert TEXT not in response.text and not voice.handles


@pytest.mark.parametrize(
    "error,status",
    [(TimeoutError("PRIVATE_SECRET"), 504), (RuntimeError(TEXT + "PRIVATE_SECRET"), 503)],
)
def test_sanitized_provider_failure(voice, error, status):
    def fail():
        raise error

    voice.rig.provider.hook = fail
    response = post(voice)
    assert response.status_code == status
    assert TEXT not in response.text and "PRIVATE_SECRET" not in response.text
    assert not voice.handles


def test_audit_failure_blocks_success(voice):
    def fail(event):
        if event["outcome"] == "success":
            raise RuntimeError("PRIVATE_SECRET")

    voice.rig.audit_hook = fail
    response = post(voice)
    assert response.status_code == 503 and TEXT not in response.text
    assert not voice.handles


def test_tts_separate_text_authorization_and_no_playback(voice):
    assert post(voice, "/synthesis", synthesis()).status_code == 403
    assert voice.rig.provider.calls == 0
    voice.rig.tts_approved = True
    response = post(voice, "/synthesis", synthesis())
    assert response.status_code == 200 and response.content == AUDIO
    assert response.headers["x-voice-playback-authorized"] == "false"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    assert post(voice, "/synthesis", synthesis(text=TEXT)).status_code == 403


def test_no_implicit_permission_from_existing_consent_purposes(voice):
    voice.rig.change(voice_notice_approved=False)
    assert (
        post(voice, payload=transcription(care=True, uploads=True, future_ai=True)).status_code
        == 422
    )
    assert post(voice).status_code == 403
    assert voice.rig.provider.calls == 0


def test_no_live_gnani_construction_on_startup_or_default_enablement(web):
    assert web.app.state.voice_bindings.service is None
    assert web.app.state.voice_bindings.capabilities == GNANI_CAPABILITIES
    assert not web.app.state.voice_bindings.enabled


def test_workflow_binding_failure_never_returns_transcript(voice):
    def fail(handle):
        raise RuntimeError("PRIVATE_SECRET " + TEXT)

    config = voice.app.state.voice_bindings
    voice.app.state.voice_bindings = replace(
        config, workflows=replace(config.workflows, remember=fail)
    )
    response = post(voice)
    assert response.status_code == 503 and TEXT not in response.text
    assert "PRIVATE_SECRET" not in response.text and not voice.handles


def test_auth_me_and_csrf_remain_unchanged(voice):
    me = voice.client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["role"] == "nurse"
    csrf = voice.client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    assert voice.rig.provider.calls == 0


def test_consent_change_in_ephemeral_remember_discards_http_result(voice):
    config = voice.app.state.voice_bindings

    def remember(handle):
        config.workflows.remember(handle)
        voice.rig.change(care_revision=uuid4())

    voice.app.state.voice_bindings = replace(
        config, workflows=replace(config.workflows, remember=remember)
    )
    response = post(voice)
    assert response.status_code == 409 and TEXT not in response.text


def test_consent_change_in_ephemeral_update_discards_http_review(voice):
    draft = get_draft(voice)
    config = voice.app.state.voice_bindings

    def update(old, new):
        config.workflows.replace(old, new)
        voice.rig.change(voice_revision=uuid4())

    voice.app.state.voice_bindings = replace(
        config, workflows=replace(config.workflows, replace=update)
    )
    response = review(voice, draft)
    assert response.status_code == 409 and CORRECTED not in response.text


@pytest.mark.parametrize(
    "field",
    [
        "authenticated",
        "active_nurse",
        "patient_assigned",
        "care_granted",
        "voice_notice_approved",
        "recording_permitted",
        "external_voice_processing_permitted",
    ],
)
def test_review_permissions_rechecked_not_inferred_from_old_handle(voice, field):
    draft = get_draft(voice)
    voice.rig.change(**{field: False})
    response = review(voice, draft)
    assert response.status_code == 403
    assert TEXT not in response.text and CORRECTED not in response.text
    assert voice.handles[next(iter(voice.handles))].transcript.state == "unverified"


@pytest.mark.parametrize(
    "field",
    [
        "authenticated",
        "active_nurse",
        "patient_assigned",
        "care_granted",
        "voice_notice_approved",
        "external_voice_processing_permitted",
    ],
)
def test_tts_separate_permissions_rechecked(voice, field):
    voice.rig.tts_approved = True
    voice.rig.change(**{field: False})
    assert post(voice, "/synthesis", synthesis()).status_code == 403
    assert voice.rig.provider.calls == 0


def test_tts_inflight_revocation_discards_audio(voice):
    voice.rig.tts_approved = True
    voice.rig.provider.hook = lambda: voice.rig.change(care_granted=False)
    response = post(voice, "/synthesis", synthesis())
    assert response.status_code == 409 and AUDIO not in response.content


@pytest.mark.parametrize("action", ["confirm", "correct"])
def test_cannot_assert_reviewer_or_submit_fake_transcript_in_review(voice, action):
    draft = get_draft(voice)
    response = review(
        voice,
        draft,
        action=action,
        corrected_text=CORRECTED if action == "correct" else None,
        nurse_id=str(uuid4()),
        transcript=TEXT,
    )
    assert response.status_code == 422 and TEXT not in response.text


def test_workflow_compare_and_swap_failure_returns_conflict_not_review(voice):
    draft = get_draft(voice)
    config = voice.app.state.voice_bindings

    def conflict(old, new):
        raise VoiceServiceError("stale")

    voice.app.state.voice_bindings = replace(
        config, workflows=replace(config.workflows, replace=conflict)
    )
    response = review(voice, draft)
    assert response.status_code == 409 and CORRECTED not in response.text


@pytest.mark.parametrize(
    "change",
    [
        lambda result: result.model_copy(update={"original_text": ""}),
        lambda result: result.model_copy(update={"state": "nurse_confirmed"}),
        lambda result: result.model_copy(update={"visit_id": uuid4()}),
    ],
)
def test_malformed_provider_draft_never_reaches_http(voice, change):
    voice.rig.provider.change = change
    response = post(voice)
    assert response.status_code == 503 and TEXT not in response.text
    assert not voice.handles


def test_json_duplicate_content_type_is_rejected(voice):
    response = voice.client.post(
        voice.root + "/transcriptions",
        content=b"{}",
        headers=[
            *headers().items(),
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
    )
    assert response.status_code == 415


def test_voice_stream_limit_does_not_change_patient_or_auth_limits(voice):
    assert voice.client.post("/api/v1/auth/login", content=b"x" * 16385).status_code == 413
    assert voice.client.post("/api/v1/patients", content=b"x" * 32769).status_code == 413
    assert voice.rig.provider.calls == 0


def test_malformed_workflow_handle_is_sanitized(voice):
    draft = get_draft(voice)
    config = voice.app.state.voice_bindings

    def invalid_handle(key):
        return replace(voice.handles[key], transcript=None)

    voice.app.state.voice_bindings = replace(
        config, workflows=replace(config.workflows, resolve=invalid_handle)
    )
    response = review(voice, draft)
    assert response.status_code == 503 and TEXT not in response.text
