from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.auth.router import router as auth_router
from app.modules.inquiries.router import router as inquiry_router
from app.modules.tenants.router import router as tenant_router
from app.modules.staff.router import router as staff_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(inquiry_router)
router.include_router(tenant_router)
router.include_router(staff_router)
from app.modules.properties.router import router as property_router
router.include_router(property_router)
from app.modules.inventory.router import router as inventory_router
router.include_router(inventory_router)
from app.modules.inventory.discovery import router as discovery_router
router.include_router(discovery_router)
from app.modules.finance.router import router as finance_router
router.include_router(finance_router)


@router.get("/health", tags=["Health"])
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "application": "ok", "database": "ok"}
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable", "application": "ok", "database": "unavailable"}, status_code=503)
