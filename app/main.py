import logging
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import router
from app.core.config import get_settings
from app.core.exceptions import install_error_handlers
from app.core.logging import configure_logging
from app.core.workflow_limits import WorkflowBodyLimit


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    app = FastAPI(title=settings.app_name, version="0.1.0", debug=False)
    install_error_handlers(app)
    app.include_router(router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = perf_counter()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Log matched route templates only: never query strings, credentials, or tokens.
        route = request.scope.get("route")
        logging.getLogger("stayops.http").info("%s %s %s %.0fms", request.method,
            getattr(route, "path", "unmatched"), response.status_code, (perf_counter() - started) * 1000)
        return response

    app.add_middleware(WorkflowBodyLimit)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-StayOps-CSRF", "Idempotency-Key"])
    return app


app = create_app()
