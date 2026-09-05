from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.auth import router as auth_router

app = FastAPI(
    title="n8n Authentication Service",
    description="Minimal, production-ready authentication backend designed for n8n workflows and PaaS deployment.",
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


@app.get(
    "/health",
    tags=["System"],
    summary="Health Check",
    description="Basic health check endpoint for container orchestrators and monitoring.",
)
def health_check():
    return {"status": "ok"}
