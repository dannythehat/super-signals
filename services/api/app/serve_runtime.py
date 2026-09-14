"""Start the Smart Signals API with universal member MT5 onboarding loaded."""

from __future__ import annotations

import os

import uvicorn

# Connection V2 extends the shared Owner account router. Import it before app.main is
# loaded so every production process exposes the same universal onboarding/reconnect
# endpoints without any account-specific runtime patching.
from app.routes import member_connection_v2 as _member_connection_v2  # noqa: F401,E402


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
