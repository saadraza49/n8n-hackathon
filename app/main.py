from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.auth import router as auth_router
from app.routers.flights import router as flights_router
from app.routers.bookings import router as bookings_router
from app.routers.waitlists import router as waitlists_router
from app.routers.automation import router as automation_router

app = FastAPI(
    title="Flight Management System API",
    description="Production-ready FastAPI backend for Flight Management System with RBAC, PostgreSQL integrity rules, and n8n integration.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for n8n workflows, webhook integrations, and client apps
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register endpoints
app.include_router(auth_router)
app.include_router(flights_router)
app.include_router(bookings_router)
app.include_router(waitlists_router)
app.include_router(automation_router)






@app.get(
    "/health",
    tags=["System"],
    summary="Health Check",
    description="Basic health check endpoint for container orchestrators and monitoring.",
)
def health_check():
    return {"status": "ok"}
