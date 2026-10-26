"""Fictional MockTransport-only contracts; no real keys, patients or approvals.

Fixture issuer constructs exact server-context bindings for tests only. It is NOT
an authentication, clinical approval, or production capability implementation.
"""

import importlib
import json
import logging
import socket
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.integrations import gemini
from app.integrations.ai_capabilities import GEMINI_CAPABILITIES
from app.schemas.ai import (
    AIContext,
    AITrustContext,
    EvidenceReference,
    InputReview,
    VerifiedClinicalInput,
)

KEY = "FICTIONAL-MOCK-CREDENTIAL-NOT-A-REAL-KEY"
PRIVATE = "FICTIONAL-PRIVATE-INPUT-NOT-PATIENT-DATA"
UID = UUID("10000000-0000-4000-8000-000000000001")
OTHER = UUID("10000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network forbidden")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def records():
    bindings = set()

    def trust():
        return AITrustContext(frozenset(bindings))

    def issue(model, **data):
        sample = model.model_construct(**data)
        bindings.add((model.__name__, sha256(sample.model_dump_json().encode()).hexdigest()))
        return model.model_validate(data, context={"ai_trust": trust()})

    context = issue(
        AIContext,
        patient_id=UID,
        visit_id=UID,
        language="en",
        input_revision=1,
        authorization_snapshot=OTHER,
        consent_snapshot=OTHER,
    )
    review = issue(
        InputReview,
        context=context,
        review_id=UID,
        input_id=UID,
        revision=1,
        content_hash=sha256(PRIVATE.encode()).hexdigest(),
        language="en",
        source="nurse_entered",
        nurse_id=UID,
        reviewed_at=NOW,
    )
    clinical = VerifiedClinicalInput.model_validate(
        dict(
            context=context,
            input_id=UID,
            revision=1,
            original_text=PRIVATE,
            original_language="en",
            source="nurse_entered",
            content_hash=review.content_hash,
            review=review,
            verification_history=(review,),
            status="nurse_verified",
        ),
        context={"ai_trust": trust()},
    )
    evidence = issue(
        EvidenceReference,
        context=context,
        evidence_id=UID,
        revision=1,
        content_hash=clinical.content_hash,
        kind="verified_input",
    )
    return clinical, evidence, trust, issue


def question(**updates):
    return (
        dict(
            language="en",
            question="FICTIONAL QUESTION ONLY",
            evidence_ids=["E1"],
            missing_information_rationale="FICTIONAL MISSING INFORMATION",
        )
        | updates
    )


def assessment(**updates):
    return (
        dict(
            observations=[
                dict(
                    text="FICTIONAL OBSERVATION",
                    evidence_ids=["E1"],
                    uncertainty="Not clinically verified",
                )
            ],
            missing_information=[],
            contradictions=[],
            limitations=["FICTIONAL ONLY"],
            doctor_summary=dict(language="en", text="FICTIONAL ENGLISH SUMMARY"),
        )
        | updates
    )


def envelope(output=None, **updates):
    return (
        dict(
            id="fictional-interaction-1",
            object="interaction",
            status="completed",
            model="gemini-3.6-flash",
            created="2026-01-02T00:00:00Z",
            updated="2026-01-02T00:00:00Z",
            steps=[
                dict(
                    type="model_output",
                    content=[
                        dict(type="text", text=json.dumps(question() if output is None else output))
                    ],
                )
            ],
        )
        | updates
    )


def response(output=None, **updates):
    return httpx.Response(200, json=envelope(output, **updates))


def prototype(reply=None, *, languages=frozenset({"en"})):
    calls = []

    def handler(request):
        calls.append(request)
        if callable(reply):
            return reply(request)
        return reply if reply is not None else response()

    adapter = gemini.create_offline_gemini_prototype(
        credential=SecretStr(KEY),
        transport=httpx.MockTransport(handler),
        languages=languages,
    )
    return adapter, calls


def invoke(adapter, records, *, operation="follow_up", **updates):
    clinical, evidence, trust, _ = records
    kwargs = dict(clinical_input=clinical, evidence=(evidence,), trust=trust()) | updates
    return (
        adapter.follow_up(turn=1, **kwargs)
        if operation == "follow_up"
        else adapter.assess(**kwargs)
    )


def safe(error, code=None):
    assert type(error.value) is gemini.GeminiError
    assert KEY not in str(error.value)
    assert PRIVATE not in str(error.value)
    assert "https://" not in str(error.value)
    assert error.value.__suppress_context__
    if code:
        assert error.value.code == code


def test_exact_documented_envelope_header_and_unreviewed_result(records, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, calls = prototype()
    result = invoke(adapter, records)
    assert result.state == "unreviewed"
    assert result.language == "en"
    assert result.evidence == (records[1],)
    assert result.context == records[0].context
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://generativelanguage.googleapis.com/v1/interactions"
    assert request.method == "POST"
    assert request.headers["x-goog-api-key"] == KEY
    assert request.headers["content-type"] == "application/json"
    assert not request.url.query
    assert KEY not in str(request.url)
    payload = json.loads(request.content)
    assert set(payload) == {
        "model",
        "input",
        "system_instruction",
        "response_format",
        "generation_config",
        "store",
        "stream",
        "background",
    }
    assert payload["model"] == "models/gemini-3.6-flash"
    assert payload["store"] is payload["stream"] is payload["background"] is False
    assert payload["generation_config"] == {
        "max_output_tokens": 2048,
        "tool_choice": "none",
        "thinking_summaries": "none",
    }
    assert payload["response_format"]["type"] == "text"
    assert payload["response_format"]["mime_type"] == "application/json"
    assert payload["response_format"]["schema"]["additionalProperties"] is False
    assert "nurse_id" not in request.content.decode()
    assert str(UID) not in request.content.decode()
    assert str(OTHER) not in request.content.decode()
    assert json.loads(payload["input"])["original_text"] == PRIVATE
    assert KEY not in caplog.text and PRIVATE not in caplog.text


def test_english_assessment_is_not_clinician_authority(records):
    adapter, calls = prototype(response(assessment()))
    draft = invoke(adapter, records, operation="assessment")
    assert draft.state == "ai_unverified"
    assert draft.doctor_summary.language == "en"
    assert draft.doctor_summary.state == "unreviewed"
    assert draft.observations[0].state == "ai_unverified"
    assert draft.observations[0].evidence_ids == (UID,)
    assert "English" in json.loads(calls[0].content)["system_instruction"]


@pytest.mark.parametrize(
    "extra",
    [
        dict(nurse_approved=True),
        dict(clinician_approved=True),
        dict(prescription="forbidden"),
        dict(triage="urgent"),
        dict(state="nurse_verified"),
        dict(context={}),
    ],
)
@pytest.mark.parametrize("operation", ["follow_up", "assessment"])
def test_provider_cannot_issue_authority(records, extra, operation):
    output = question() if operation == "follow_up" else assessment()
    adapter, calls = prototype(response(output | extra))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records, operation=operation)
    safe(error, "invalid_response")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        dict(language="hi"),
        dict(language="fr"),
        dict(question=""),
        dict(question="x" * 1001),
        dict(question="x\x00y"),
        dict(evidence_ids=["E999"]),
        dict(evidence_ids=["E1", "E1"]),
        dict(evidence_ids=["https://not-allowed.invalid"]),
    ],
)
def test_invalid_question_and_evidence(records, changes):
    adapter, _ = prototype(response(question(**changes)))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error)
    assert error.value.code in {"invalid_response", "unauthorized_evidence"}


@pytest.mark.parametrize(
    "changes",
    [
        dict(observations=[dict(text="FICTIONAL", evidence_ids=["E999"], uncertainty="Unknown")]),
        dict(
            observations=[dict(text="FICTIONAL", evidence_ids=["E1", "E1"], uncertainty="Unknown")]
        ),
        dict(doctor_summary=dict(language="hi", text="FICTIONAL")),
        dict(doctor_summary=dict(language="en", text="FICTIONAL", approved=True)),
        dict(limitations=[]),
        dict(missing_information=["x"] * 21),
    ],
)
def test_invalid_assessment(records, changes):
    adapter, _ = prototype(response(assessment(**changes)))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records, operation="assessment")
    safe(error)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "incomplete"),
        ("status", "requires_action"),
        ("status", "failed"),
        ("status", "in_progress"),
        ("model", "gemini-flash-latest"),
        ("object", "other"),
        ("updated", "2025-01-02T00:00:00Z"),
        ("steps", []),
        ("steps", [{"type": "function_call", "name": "forbidden"}]),
        ("steps", [{"type": "model_output", "content": [{"type": "image", "data": "forbidden"}]}]),
        ("unknown", "forbidden"),
        ("usage", {"total_tool_use_tokens": 1}),
    ],
)
def test_strict_envelope_and_tool_rejection(records, field, value):
    adapter, _ = prototype(response(**{field: value}))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize(
    "field", ["id", "model", "object", "status", "created", "updated", "steps"]
)
def test_missing_envelope_fields(records, field):
    data = envelope()
    del data[field]
    adapter, _ = prototype(httpx.Response(200, json=data))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"{}",
        b'{"status":"completed","status":"failed"}',
        b'{"status":NaN}',
        b"\xff",
        b'{"arbitrary":Infinity}',
    ],
)
def test_malformed_outer_json(records, body):
    adapter, _ = prototype(
        httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize(
    "text",
    [
        '{"question":"FICTIONAL","question":"DUPLICATE"}',
        "null",
        "[]",
        '{"question":NaN}',
        '{"question":Infinity}',
        "{",
        "```json\n{}\n```",
    ],
)
def test_malformed_nested_json(records, text):
    data = envelope()
    data["steps"][0]["content"][0]["text"] = text
    adapter, _ = prototype(httpx.Response(200, json=data))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "provider_unavailable"),
        (302, "provider_unavailable"),
        (307, "provider_unavailable"),
        (308, "provider_unavailable"),
        (400, "provider_blocked"),
        (401, "provider_unavailable"),
        (403, "provider_unavailable"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
        (504, "provider_unavailable"),
    ],
)
def test_status_errors_never_retry_redirect_or_expose_provider_content(records, status, code):
    adapter, calls = prototype(
        httpx.Response(
            status, text=KEY + PRIVATE, headers={"Location": "https://not-allowed.invalid"}
        )
    )
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    # Deliberate safe errors have no underlying third-party exception.
    assert error.value.code == code
    assert KEY not in str(error.value) and PRIVATE not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "exception,code",
    [
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.WriteTimeout, "timeout"),
        (httpx.PoolTimeout, "timeout"),
        (httpx.ConnectError, "provider_unavailable"),
        (RuntimeError, "provider_unavailable"),
    ],
)
def test_transport_failure_is_sanitized(records, exception, code):
    def fail(request):
        raise exception(KEY + PRIVATE)

    adapter, calls = prototype(fail)
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, code)
    assert len(calls) == 1


def test_timeouts_and_no_environment_proxy_inheritance(records, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://fictional-proxy.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://fictional-proxy.invalid:9999")
    real = httpx.Client
    constructed = []

    def client(*args, **kwargs):
        constructed.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    adapter, calls = prototype()
    invoke(adapter, records)
    assert len(calls) == 1
    config = constructed[0]
    assert config["trust_env"] is False
    assert config["follow_redirects"] is False
    assert "proxy" not in config
    assert config["timeout"].as_dict() == dict(connect=5, read=20, write=10, pool=5)


def test_production_is_always_blocked_and_import_does_not_send(records, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("Client must not be constructed")

    monkeypatch.setattr(httpx, "Client", forbidden)
    importlib.reload(gemini)
    for kwargs in [
        {},
        dict(enabled=True),
        dict(transport=httpx.MockTransport(lambda r: response())),
    ]:
        with pytest.raises(gemini.GeminiError, match="capability_disabled"):
            gemini.GeminiAdapter(**kwargs)
    instance = object.__new__(gemini.GeminiAdapter)
    for method in [instance.follow_up, instance.assess]:
        with pytest.raises(gemini.GeminiError, match="capability_disabled"):
            method()
    assert calls == []
    assert GEMINI_CAPABILITIES.declarations == ()


@pytest.mark.parametrize(
    "updates",
    [
        dict(credential=None),
        dict(credential=KEY),
        dict(credential=SecretStr("")),
        dict(credential=SecretStr("bad\nheader")),
        dict(transport=None),
        dict(transport=object()),
        dict(languages=frozenset({"fr"})),
        dict(languages={"en"}),
    ],
)
def test_factory_rejects_unsafe_configuration(updates):
    kwargs = (
        dict(
            credential=SecretStr(KEY),
            transport=httpx.MockTransport(lambda r: response()),
            languages=frozenset({"en"}),
        )
        | updates
    )
    with pytest.raises(gemini.GeminiError) as error:
        gemini.create_offline_gemini_prototype(**kwargs)
    safe(error, "invalid_configuration")


def test_no_inherited_credentials_or_default_language_support(records, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    adapter, calls = prototype(languages=frozenset())
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    assert error.value.code == "unsupported_language"
    assert calls == []


def test_untrusted_inputs_cannot_transmit(records):
    adapter, calls = prototype()
    for updates in [
        dict(trust=None),
        dict(evidence=(records[1],) * 2),
        dict(clinical_input={"original_text": PRIVATE}),
    ]:
        with pytest.raises(gemini.GeminiError) as error:
            invoke(adapter, records, **updates)
        safe(error, "invalid_request")
    assert calls == []


def test_multimodal_evidence_cannot_transmit(records):
    clinical, evidence, trust, issue = records
    image = issue(
        EvidenceReference,
        **(evidence.model_dump() | {"context": clinical.context, "kind": "image"}),
    )
    adapter, calls = prototype()
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records, evidence=(image,))
    assert error.value.code == "unsupported_modality"
    assert calls == []


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def test_response_stream_limit_and_cleanup(records):
    stream = Chunks([b"x" * 8192] * 20)
    adapter, _ = prototype(
        httpx.Response(200, stream=stream, headers={"content-type": "application/json"})
    )
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    assert error.value.code == "response_too_large"
    assert stream.closed and stream.reads == 9


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/plain"},
        {"content-encoding": "gzip"},
        {"content-length": str(gemini.MAX_RESPONSE_BYTES + 1)},
        {"content-length": "invalid"},
        {"content-length": "9999999999999999"},
        [("content-length", "1"), ("content-length", "2")],
    ],
)
def test_response_headers_fail_before_body_read(records, headers):
    combined = [("content-type", "application/json")]
    combined += list(headers.items()) if isinstance(headers, dict) else headers
    stream = Chunks([b"forbidden"])
    adapter, _ = prototype(httpx.Response(200, stream=stream, headers=combined))
    with pytest.raises(gemini.GeminiError):
        invoke(adapter, records)
    assert stream.closed and stream.reads == 0


def test_request_size_limit_precedes_http(records, monkeypatch):
    monkeypatch.setattr(gemini, "MAX_REQUEST_BYTES", 1)
    adapter, calls = prototype()
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    assert error.value.code == "request_too_large"
    assert calls == []


def test_content_length_mismatch(records):
    raw = json.dumps(envelope()).encode()
    stream = Chunks([raw])
    adapter, _ = prototype(
        httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "application/json", "content-length": str(len(raw) + 1)},
        )
    )
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")
    assert stream.closed


def test_duplicate_keys_rejected_even_if_otherwise_valid(records):
    valid = json.dumps(envelope())
    duplicated = valid.replace(
        '"status": "completed"', '"status": "completed", "status": "completed"'
    )
    nested = envelope()
    text = json.dumps(question())
    nested["steps"][0]["content"][0]["text"] = text.replace(
        '"language": "en"', '"language": "en", "language": "en"'
    )
    for body in [duplicated.encode(), json.dumps(nested).encode()]:
        adapter, _ = prototype(
            httpx.Response(200, content=body, headers={"content-type": "application/json"})
        )
        with pytest.raises(gemini.GeminiError) as error:
            invoke(adapter, records)
        safe(error, "invalid_response")


def test_documented_usage_subset_and_exact_model_profile(records):
    usage = dict(
        total_input_tokens=7,
        total_output_tokens=20,
        total_thought_tokens=22,
        total_cached_tokens=0,
        total_tool_use_tokens=0,
        total_tokens=49,
        input_tokens_by_modality=[dict(modality="text", tokens=7)],
    )
    adapter, _ = prototype(response(usage=usage, model="models/gemini-3.6-flash"))
    assert invoke(adapter, records).state == "unreviewed"
    adapter, _ = prototype(response(usage={"total_tool_use_tokens": False}))
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


def test_midstream_timeout_closes_response(records):
    class TimeoutStream(Chunks):
        def __iter__(self):
            yield b"{"
            raise httpx.ReadTimeout(KEY + PRIVATE)

    stream = TimeoutStream([])
    adapter, calls = prototype(
        httpx.Response(200, stream=stream, headers={"content-type": "application/json"})
    )
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    safe(error, "timeout")
    assert len(calls) == 1 and stream.closed


def test_elapsed_bound_rejects_late_result(records, monkeypatch):
    times = iter([0.0, 31.0])
    monkeypatch.setattr(gemini, "monotonic", lambda: next(times))
    adapter, calls = prototype()
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records)
    assert error.value.code == "timeout" and len(calls) == 1


def test_subclassed_or_live_transport_is_not_an_offline_seam():
    class Subclass(httpx.MockTransport):
        pass

    for transport in [httpx.HTTPTransport(), Subclass(lambda r: response())]:
        try:
            with pytest.raises(gemini.GeminiError) as error:
                gemini.create_offline_gemini_prototype(
                    credential=SecretStr(KEY), transport=transport, languages=frozenset({"en"})
                )
            safe(error, "invalid_configuration")
        finally:
            transport.close()


def test_no_environment_or_runtime_model_endpoint_prompt_overrides():
    for extra in [
        dict(enabled=True),
        dict(model="arbitrary"),
        dict(endpoint="arbitrary"),
        dict(system_instruction="arbitrary"),
        dict(proxy="arbitrary"),
    ]:
        with pytest.raises(TypeError):
            gemini.create_offline_gemini_prototype(
                credential=SecretStr(KEY),
                transport=httpx.MockTransport(lambda r: response()),
                languages=frozenset({"en"}),
                **extra,
            )


def test_assessment_does_not_silently_assume_english_capability(records):
    clinical, evidence, trust, issue = records
    context = issue(AIContext, **(clinical.context.model_dump() | {"language": "hi"}))
    review = issue(
        InputReview, **(clinical.review.model_dump() | {"context": context, "language": "hi"})
    )
    revised = VerifiedClinicalInput.model_validate(
        clinical.model_dump()
        | {
            "context": context,
            "original_language": "hi",
            "review": review,
            "verification_history": (review,),
        },
        context={"ai_trust": trust()},
    )
    source = issue(EvidenceReference, **(evidence.model_dump() | {"context": context}))
    adapter, calls = prototype(languages=frozenset({"hi"}))
    with pytest.raises(gemini.GeminiError) as error:
        adapter.assess(clinical_input=revised, evidence=(source,), trust=trust())
    assert error.value.code == "unsupported_language" and calls == []
    adapter, calls = prototype(response(question(language="hi")), languages=frozenset({"hi"}))
    result = adapter.follow_up(clinical_input=revised, evidence=(source,), turn=1, trust=trust())
    assert result.language == "hi" and result.state == "unreviewed" and len(calls) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"original_text": "x" * 8001},
        {"nurse_approved": True},
        {"status": "unverified"},
    ],
)
def test_invalid_or_oversized_clinical_input_is_rejected_before_http(records, updates):
    adapter, calls = prototype()
    data = records[0].model_dump() | updates
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records, clinical_input=data)
    safe(error, "invalid_request")
    assert calls == []


def test_trusted_evidence_from_another_visit_is_rejected_before_http(records):
    clinical, evidence, trust, issue = records
    context = issue(AIContext, **(clinical.context.model_dump() | {"visit_id": OTHER}))
    source = issue(EvidenceReference, **(evidence.model_dump() | {"context": context}))
    adapter, calls = prototype()
    with pytest.raises(gemini.GeminiError) as error:
        invoke(adapter, records, evidence=(source,))
    safe(error, "invalid_request")
    assert calls == []
