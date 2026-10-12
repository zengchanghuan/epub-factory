"""A response owns its temporary file until streaming finishes or fails."""
from pathlib import Path

from starlette.responses import FileResponse


class TemporaryFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        # Stream the bytes ourselves: handing off a path to the server could
        # return before it opens the file, making immediate deletion unsafe.
        extensions = dict(scope.get("extensions", {}))
        extensions.pop("http.response.pathsend", None)
        scope = {**scope, "extensions": extensions}
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)
