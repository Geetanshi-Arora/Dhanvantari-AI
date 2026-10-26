"""Fictional, mock-only contract tests; no installed model or clinical approvals."""

import importlib
import json
import logging
import socket
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import httpx
import pytest

from app.integrations import ollama
from app.integrations.ai_capabilities import GEMINI_CAPABILITIES
from app.integrations.gemini import GeminiAdapter, GeminiError
from app.integrations.ollama_capabilities import (
    OLLAMA_CAPABILITIES,
    OllamaCapability,
    OllamaCapabilityRegistry,
)
from app.schemas.ai import (
    AIContext,
    AITrustContext,
    EvidenceReference,
    InputReview,
    VerifiedClinicalInput,
)

UID = UUID("10000000-0000-4000-8000-000000000001")
OTHER = UUID("10000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 1, 2, tzinfo=UTC)
PRIVATE = "FICTIONAL ADULT: no fever reported."
DIGEST = "a" * 64  # Synthetic, NOT a downloaded artifact hash.
LANGUAGES = ("en", "hi", "bn", "ta", "te", "mr", "gu", "pa")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network/inference forbidden")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def records():
    bindings = set()

    def trust():
        return AITrustContext(frozenset(bindings))

    def issue(model, **values):
        sample = model.model_construct(**values)
        bindings.add((model.__name__, sha256(sample.model_dump_json().encode()).hexdigest()))
        return model.model_validate(values, context={"ai_trust": trust()})

    def source(language="en", text=PRIVATE):
        context = issue(
            AIContext,
            patient_id=UID,
            visit_id=UID,
            language=language,
            input_revision=1,
            authorization_snapshot=OTHER,
            consent_snapshot=OTHER,
        )
        content_hash = sha256(text.encode()).hexdigest()
        review = issue(
            InputReview,
            context=context,
            review_id=UID,
            input_id=UID,
            revision=1,
            content_hash=content_hash,
            language=language,
            source="nurse_entered",
            nurse_id=UID,
            reviewed_at=NOW,
        )
        clinical = VerifiedClinicalInput.model_validate(
            dict(
                context=context,
                input_id=UID,
                revision=1,
                original_text=text,
                original_language=language,
                content_hash=content_hash,
                source="nurse_entered",
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
            content_hash=content_hash,
            kind="verified_input",
        )
        return clinical, evidence

    clinical, evidence = source()
    return clinical, evidence, trust, issue, source


def policy():
    declarations = [
        OllamaCapability(
            operation="question_generation",
            language=language,
            modality="text",
            model=ollama.OFFLINE_ALIAS,
            artifact_digest=DIGEST,
            documentation_reference="fictional-offline-documentation",
            approval_reference="fictional-reference-not-human-approval",
        )
        for language in LANGUAGES
    ]
    declarations.append(
        OllamaCapability(
            operation="clinical_observations",
            language="en",
            modality="text",
            model=ollama.OFFLINE_ALIAS,
            artifact_digest=DIGEST,
            documentation_reference="fictional-offline-documentation",
            approval_reference="fictional-reference-not-human-approval",
        )
    )
    return OllamaCapabilityRegistry(declarations=tuple(declarations))


def candidates():
    # Labels are fictional metadata, NOT approved translations or clinical questions.
    return tuple(
        ollama.OfflineQuestionCandidate(
            candidate_id="fixture-question",
            language=language,
            text="FICTIONAL QUESTION ONLY (" + language + ")",
        )
        for language in LANGUAGES
    )


def question(**changes):
    return (
        dict(
            candidate_id="fixture-question",
            language="en",
            question=candidates()[0].text,
            evidence_ids=["E1"],
        )
        | changes
    )


def summary(**changes):
    return dict(language="en", text=PRIVATE, evidence_ids=["E1"]) | changes


def envelope(output=None, **changes):
    return (
        dict(
            model=ollama.OFFLINE_MODEL,
            created_at="2026-01-02T00:00:00Z",
            message={
                "role": "assistant",
                "content": json.dumps(question() if output is None else output),
            },
            done=True,
            done_reason="stop",
        )
        | changes
    )


def response(output=None, **changes):
    return httpx.Response(200, json=envelope(output, **changes))


def prototype(reply=None, **changes):
    calls = []

    def handler(request):
        calls.append(request)
        return reply(request) if callable(reply) else (reply if reply is not None else response())

    options = (
        dict(
            transport=httpx.MockTransport(handler),
            artifact_digest=DIGEST,
            registry=policy(),
            questions=candidates(),
        )
        | changes
    )
    return ollama.create_offline_ollama_prototype(**options), calls


def invoke(adapter, records, *, operation="follow_up", **changes):
    clinical, evidence, trust, _, _ = records
    kwargs = dict(clinical_input=clinical, evidence=(evidence,), trust=trust()) | changes
    return (
        adapter.follow_up(turn=1, **kwargs)
        if operation == "follow_up"
        else adapter.assess(**kwargs)
    )


def safe(error, code=None):
    assert type(error.value) is ollama.OllamaError
    assert PRIVATE not in str(error.value)
    assert "http://" not in str(error.value)
    assert "SECRET-FIXTURE" not in str(error.value)
    assert error.value.__suppress_context__
    if code:
        assert error.value.code == code


def test_exact_mock_contract_preserves_original_and_unreviewed_state(records, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, calls = prototype()
    draft = invoke(adapter, records)
    assert draft.context == records[0].context and draft.language == "en"
    assert draft.state == "unreviewed" and draft.evidence == (records[1],)
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST" and str(request.url) == "http://127.0.0.1:11434/api/chat"
    assert not request.url.query and "authorization" not in request.headers
    payload = json.loads(request.content)
    assert set(payload) == {
        "model",
        "messages",
        "format",
        "stream",
        "think",
        "keep_alive",
        "options",
    }
    assert payload["model"] == "fictional-offline-model:fixture"
    assert payload["stream"] is False and payload["think"] is False
    assert payload["keep_alive"] == 0
    assert payload["options"] == {"num_ctx": 2048, "num_predict": 256, "temperature": 0}
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    original = json.loads(payload["messages"][1]["content"])
    assert original["original_text"] == PRIVATE and original["original_language"] == "en"
    assert str(UID) not in request.content.decode() and str(OTHER) not in request.content.decode()
    assert payload["format"]["additionalProperties"] is False
    assert PRIVATE not in caplog.text and draft.question not in caplog.text
    assert PRIVATE not in repr(adapter)


def test_extractive_english_summary_is_not_a_clinical_decision(records):
    adapter, _ = prototype(response(summary()))
    result = invoke(adapter, records, operation="assessment")
    assert result.state == "ai_unverified" and result.doctor_summary.state == "unreviewed"
    assert result.doctor_summary.text == records[0].original_text
    assert result.doctor_summary.language == "en"
    assert result.observations[0].text == PRIVATE
    assert result.observations[0].evidence_ids == (UID,)
    assert "not a clinical assessment" in result.limitations[0]


@pytest.mark.parametrize(
    "text",
    [
        "FICTIONAL ADULT: fever reported.",
        "fever reported.",
        "FICTIONAL invented diagnosis",
        PRIVATE + " Added fact.",
    ],
)
def test_invented_or_rewritten_summary_facts_are_rejected(records, text):
    adapter, _ = prototype(response(summary(text=text)))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records, operation="assessment")
    safe(error, "unsupported_facts")


@pytest.mark.parametrize(
    "changes",
    [
        dict(question="Do you still have the invented fever?"),
        dict(candidate_id="invented-question"),
        dict(language="hi"),
    ],
)
def test_generated_question_facts_or_unlisted_text_are_rejected(records, changes):
    adapter, _ = prototype(response(question(**changes)))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "unsupported_facts")


@pytest.mark.parametrize(
    "extra",
    [
        dict(nurse_approved=True),
        dict(clinician_approved=True),
        dict(prescription="forbidden"),
        dict(triage="urgent"),
        dict(state="verified"),
        dict(patient_facts=["invented"]),
    ],
)
@pytest.mark.parametrize("operation", ["follow_up", "assessment"])
def test_provider_authority_and_patient_fact_fields_are_rejected(records, extra, operation):
    output = question() if operation == "follow_up" else summary()
    adapter, _ = prototype(response(output | extra))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records, operation=operation)
    safe(error, "invalid_response")


@pytest.mark.parametrize("refs", [["E999"], ["E1", "E1"], [str(OTHER)], ["remote-url"]])
@pytest.mark.parametrize("operation", ["follow_up", "assessment"])
def test_fabricated_or_duplicate_evidence_is_rejected(records, refs, operation):
    output = question(evidence_ids=refs) if operation == "follow_up" else summary(evidence_ids=refs)
    adapter, _ = prototype(response(output))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records, operation=operation)
    safe(error, "unauthorized_evidence")


@pytest.mark.parametrize("language", LANGUAGES)
def test_language_identity_has_no_fallback_and_no_support_claim(records, language):
    clinical, evidence = records[4](language=language)
    fixture = next(item for item in candidates() if item.language == language)
    adapter, _ = prototype(response(question(language=language, question=fixture.text)))
    result = adapter.follow_up(
        clinical_input=clinical, evidence=(evidence,), turn=1, trust=records[2]()
    )
    assert result.language == language and result.question == fixture.text


def test_unreviewed_translation_cannot_be_used_for_english_summary(records):
    clinical, evidence = records[4](language="hi")
    adapter, calls = prototype(response(summary()))
    with pytest.raises(ollama.OllamaError) as error:
        adapter.assess(clinical_input=clinical, evidence=(evidence,), trust=records[2]())
    safe(error, "unsupported_language")
    assert calls == []


def test_non_english_summary_metadata_is_rejected(records):
    adapter, _ = prototype(response(summary(language="hi")))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records, operation="assessment")
    safe(error, "invalid_response")


@pytest.mark.parametrize(
    "changes",
    [
        dict(done=False),
        dict(done=1),
        dict(done_reason="length"),
        dict(model="qwen3:1.7b"),
        dict(model="fixture-cloud"),
        dict(extra="forbidden"),
        dict(message={"role": "user", "content": "{}"}),
        dict(message={"role": "assistant", "content": "{}", "tool_calls": []}),
    ],
)
def test_strict_response_completion_model_and_tool_rejection(records, changes):
    adapter, _ = prototype(response(**changes))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize("field", ["model", "created_at", "message", "done", "done_reason"])
def test_missing_envelope_fields(records, field):
    data = envelope()
    del data[field]
    adapter, _ = prototype(httpx.Response(200, json=data))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b"\xff", b'{"done":NaN}'])
def test_malformed_outer_json(records, body):
    adapter, _ = prototype(
        httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


@pytest.mark.parametrize(
    "text",
    [
        "null",
        "[]",
        "{",
        '{"question":Infinity}',
        "```json\n{}\n```",
        json.dumps(question(question="x" * 1001)),
        json.dumps(question(language="fr")),
        json.dumps(question(question="x\x00y")),
    ],
)
def test_invalid_nested_json_and_bounds(records, text):
    adapter, _ = prototype(response(message={"role": "assistant", "content": text}))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "invalid_response")


def test_duplicate_keys_are_rejected_even_if_the_values_match(records):
    raw = json.dumps(envelope()).replace('"done": true', '"done": true, "done": true')
    nested = json.dumps(question()).replace(
        '"language": "en"', '"language": "en", "language": "en"'
    )
    for body in [
        raw.encode(),
        json.dumps(envelope(message={"role": "assistant", "content": nested})).encode(),
    ]:
        adapter, _ = prototype(
            httpx.Response(200, content=body, headers={"content-type": "application/json"})
        )
        with pytest.raises(ollama.OllamaError) as error:
            invoke(adapter, records)
        safe(error, "invalid_response")


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 429, 500, 503])
def test_error_statuses_are_sanitized_without_retry_or_redirect(records, status, caplog):
    caplog.set_level(logging.DEBUG)
    adapter, calls = prototype(
        httpx.Response(
            status, text=PRIVATE, headers={"location": "https://fictional-remote.invalid"}
        )
    )
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "rate_limited" if status == 429 else "provider_unavailable")
    assert len(calls) == 1 and PRIVATE not in caplog.text


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        RuntimeError,
    ],
)
def test_transport_errors_never_leak_or_retry(records, exception):
    def fail(request):
        raise exception(PRIVATE + " SECRET-FIXTURE")

    adapter, calls = prototype(fail)
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(
        error,
        "timeout" if issubclass(exception, httpx.TimeoutException) else "provider_unavailable",
    )
    assert len(calls) == 1


def test_production_import_construction_and_execution_make_zero_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Production must not construct an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    imported = importlib.reload(ollama)
    for kwargs in [{}, dict(enabled=True), dict(endpoint="http://127.0.0.1:11434/api/chat")]:
        with pytest.raises(imported.OllamaError, match="capability_disabled"):
            imported.OllamaAdapter(**kwargs)
    instance = object.__new__(imported.OllamaAdapter)
    for method in [instance.assess, instance.follow_up]:
        with pytest.raises(imported.OllamaError, match="capability_disabled"):
            method()
    assert OLLAMA_CAPABILITIES.declarations == ()


@pytest.mark.parametrize(
    "extra",
    [
        dict(endpoint="https://remote.invalid"),
        dict(host="0.0.0.0"),
        dict(proxy="http://proxy.invalid"),
        dict(model="qwen3:1.7b"),
        dict(enabled=True),
        dict(credential="SECRET-FIXTURE"),
        dict(system_instruction="caller instruction"),
    ],
)
def test_no_endpoint_proxy_cloud_or_activation_configuration(extra):
    with pytest.raises(TypeError):
        ollama.create_offline_ollama_prototype(
            transport=httpx.MockTransport(lambda r: response()), artifact_digest=DIGEST, **extra
        )


def test_live_and_subclassed_transports_are_rejected():
    class Subclass(httpx.MockTransport):
        pass

    for transport in [httpx.HTTPTransport(), Subclass(lambda r: response()), None, object()]:
        try:
            with pytest.raises(ollama.OllamaError) as error:
                ollama.create_offline_ollama_prototype(transport=transport, artifact_digest=DIGEST)
            safe(error, "invalid_configuration")
        finally:
            if isinstance(transport, httpx.BaseTransport):
                transport.close()


def test_registry_stays_empty_and_default_test_policy_cannot_send(records):
    calls = []
    adapter = ollama.create_offline_ollama_prototype(
        transport=httpx.MockTransport(lambda r: calls.append(r)),
        artifact_digest=DIGEST,
        questions=candidates(),
    )
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "unsupported_capability")
    assert calls == [] and OLLAMA_CAPABILITIES.declarations == ()


def test_wrong_artifact_or_missing_candidates_cannot_send(records):
    for changes in [dict(artifact_digest="b" * 64), dict(questions=())]:
        adapter, calls = prototype(**changes)
        with pytest.raises(ollama.OllamaError) as error:
            invoke(adapter, records)
        safe(error, "unsupported_capability")
        assert calls == []


def test_httpx_timeouts_and_proxy_environment_are_not_inherited(records, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://fictional-remote.invalid")
    monkeypatch.setenv("ALL_PROXY", "http://fictional-remote.invalid")
    monkeypatch.setenv("OLLAMA_HOST", "https://fictional-remote.invalid")
    monkeypatch.setenv("OLLAMA_MODEL", "cloud-model")
    actual = httpx.Client
    configs = []

    def client(*args, **kwargs):
        configs.append(kwargs)
        return actual(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    adapter, calls = prototype()
    invoke(adapter, records)
    assert len(calls) == 1 and str(calls[0].url) == ollama.ENDPOINT
    assert configs[0]["trust_env"] is False and configs[0]["follow_redirects"] is False
    assert configs[0]["timeout"].as_dict() == dict(connect=5, read=20, write=10, pool=5)


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed, self.reads = chunks, False, 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def test_bounded_stream_stops_and_closes(records):
    stream = Chunks([b"x" * 8192] * 20)
    adapter, _ = prototype(
        httpx.Response(200, stream=stream, headers={"content-type": "application/json"})
    )
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "response_too_large")
    assert stream.closed and stream.reads == 9


@pytest.mark.parametrize(
    "headers",
    [
        [("content-type", "text/plain")],
        [("content-encoding", "gzip")],
        [("content-length", str(ollama.MAX_RESPONSE_BYTES + 1))],
        [("content-length", "invalid")],
        [("content-length", "1"), ("content-length", "2")],
    ],
)
def test_unsafe_headers_do_not_read_body(records, headers):
    stream = Chunks([b"forbidden"])
    adapter, _ = prototype(
        httpx.Response(200, stream=stream, headers=[("content-type", "application/json"), *headers])
    )
    with pytest.raises(ollama.OllamaError):
        invoke(adapter, records)
    assert stream.closed and stream.reads == 0


def test_request_bound_applies_before_mock_send(records, monkeypatch):
    monkeypatch.setattr(ollama, "MAX_REQUEST_BYTES", 1)
    adapter, calls = prototype()
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records)
    safe(error, "request_too_large")
    assert calls == []


def test_untrusted_and_oversized_inputs_do_not_send(records):
    adapter, calls = prototype()
    for changes in [
        dict(trust=None),
        dict(evidence=(records[1],) * 2),
        dict(clinical_input=records[0].model_dump() | {"original_text": "x" * 8001}),
    ]:
        with pytest.raises(ollama.OllamaError) as error:
            invoke(adapter, records, **changes)
        safe(error, "invalid_request")
    assert calls == []


def test_evidence_with_fabricated_hash_or_other_visit_does_not_send(records):
    clinical, evidence, trust, issue, _ = records
    alternate = issue(
        EvidenceReference,
        **(evidence.model_dump() | {"context": clinical.context, "content_hash": "b" * 64}),
    )
    adapter, calls = prototype()
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records, evidence=(alternate,))
    safe(error, "unsupported_evidence")
    context = issue(AIContext, **(clinical.context.model_dump() | {"visit_id": OTHER}))
    foreign = issue(EvidenceReference, **(evidence.model_dump() | {"context": context}))
    with pytest.raises(ollama.OllamaError) as error:
        invoke(adapter, records, evidence=(foreign,))
    safe(error, "invalid_request")
    assert calls == []


def test_gemini_behavior_unchanged():
    assert GEMINI_CAPABILITIES.declarations == ()
    with pytest.raises(GeminiError, match="capability_disabled"):
        GeminiAdapter()
