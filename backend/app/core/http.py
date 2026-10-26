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
