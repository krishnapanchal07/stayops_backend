from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": {"message": str(exc.detail)}}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo submitted passwords, tokens, or Pydantic input values.
        fields = [".".join(str(part) for part in err["loc"]) for err in exc.errors()]
        return JSONResponse({"error": {"message": "Invalid request. Check the submitted fields.", "fields": fields}}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def conflict(request: Request, exc: IntegrityError):
        return JSONResponse({"error": {"message": "The requested record conflicts with an existing record."}}, status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return JSONResponse({"error": {"message": "Service temporarily unavailable."}}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return JSONResponse({"error": {"message": "An unexpected error occurred."}}, status_code=500)
