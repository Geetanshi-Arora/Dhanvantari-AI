"""Bound authentication request bodies, including chunked requests without Content-Length."""

from starlette.responses import JSONResponse


class AuthBodyLimit:
    prefix = "/api/v1/auth"

    def __init__(self, app, limit=16384):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope["path"].startswith(self.prefix)
            or scope["method"] not in {"POST", "PUT", "PATCH"}
        ):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                return await JSONResponse({"detail": "Request too large"}, 413)(
                    scope, receive, send
                )
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class PatientBodyLimit(AuthBodyLimit):
    """Bound actual streamed patient writes before JSON parsing or authentication."""

    prefix = "/api/v1/patients"

    def __init__(self, app):
        super().__init__(app, limit=32768)


class VoiceBodyLimit(AuthBodyLimit):
    """Bound base64 audio to 4 MiB decoded, plus small JSON metadata overhead.

    All other voice writes have a smaller 32 KiB ceiling. Both bounds count the
    actual ASGI stream and do not trust Content-Length or transfer encoding.
    """

    prefix = "/api/v1/voice"
    audio_json_limit = 4 * ((4 * 1024 * 1024 + 2) // 3) + 16384

    def __init__(self, app):
        super().__init__(app, limit=32768)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].endswith("/transcriptions"):
            audio = AuthBodyLimit(self.app, limit=self.audio_json_limit)
            audio.prefix = self.prefix
            return await audio(scope, receive, send)
        return await super().__call__(scope, receive, send)
