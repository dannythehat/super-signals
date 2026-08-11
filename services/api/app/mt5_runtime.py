"""Helpers for safe Day 22 MT5 runtime access."""

from fastapi import HTTPException, Request, status


def require_mt5_service(request: Request):
    service = getattr(request.app.state, "mt5_connection_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "mt5_encryption_not_configured",
                "message": "The MT5 connection service is not configured yet.",
            },
        )
    return service
