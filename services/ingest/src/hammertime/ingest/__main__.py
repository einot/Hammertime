"""Entry point: build the app, wire bus/store, serve.

Spec: section 4; ADR-0007

Bus/store wiring is Epic #4's job; this currently just builds and serves
the validation-only app from `app.py`.

`proxy_headers=False`: uvicorn defaults to `proxy_headers=True`, which
installs its own `ProxyHeadersMiddleware` that can rewrite `scope["client"]`
from `X-Forwarded-For`/`X-Real-IP` for any peer in `forwarded_allow_ips`
(itself defaulting to `127.0.0.1,::1`, or `*` if a deployment sets
`FORWARDED_ALLOW_IPS`). That would let uvicorn silently override the
"trusted" client address behind `auth/middleware.py`'s back even when
`HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS=0` -- two independent, potentially
disagreeing layers deciding whether to trust `X-Forwarded-For`. XFF
interpretation must be owned solely by `middleware.py`'s
`trusted_proxy_hops` logic (spec section 36.5), so uvicorn's own rewriting
is disabled here.
"""

import uvicorn
from hammertime.ingest.app import create_app
from hammertime.ingest.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
