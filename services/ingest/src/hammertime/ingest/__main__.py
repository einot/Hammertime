"""Entry point: build the app, wire bus/store, serve.

Spec: section 4

Bus/store wiring is Epic #4's job; this currently just builds and serves
the validation-only app from `app.py`.
"""

import uvicorn
from hammertime.ingest.app import create_app
from hammertime.ingest.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
