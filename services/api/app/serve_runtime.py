"""Start the Smart Signals API with the canonical member Connection V2 routes loaded."""

from __future__ import annotations

import os

import uvicorn

# Import before app.main is loaded so the V2 endpoints are attached to the shared
# /admin/accounts router. No runtime monkeypatching of MT5 connection behavior.
from app.routes import member_connection_v2 as _member_connection_v2  # noqa: F401,E402


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
