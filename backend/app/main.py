import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.routes import router as api_router
from app.audit.router import router as audit_router
from app.config import get_settings
from app.decision.router import router as decision_router
from app.detect.router import router as detect_router
from app.errors import install_error_handlers
from app.graph.client import GraphClient, GraphError
from app.graph.schema import apply_schema
from app.ingest.paypal_client import PayPalClient
from app.ingest.paypal_webhook import router as paypal_router
from app.ingest.router import router as ingest_router
from app.logging_utils import configure_logging
from app.screening.router import router as screening_router

logger = logging.getLogger("app")


class Health(BaseModel):
    status: str
    graph: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Task 5: ONE GraphClient for the whole app; the schema goes through it.

    Task 8: the PayPal client is made first. It refuses anything that is not
    the PayPal sandbox, so the app stops instead of talking to production.
    """
    configure_logging()
    settings = get_settings()
    paypal = PayPalClient()
    Path(settings.relate_path).parent.mkdir(parents=True, exist_ok=True)
    client = GraphClient(
        settings.relate_path,
        query_dir=settings.graph_query_dir,
        default_limit=settings.graph_row_limit,
        default_timeout_s=settings.graph_timeout_s,
    )
    try:
        await apply_schema(client)
        app.state.graph = client
        app.state.paypal = paypal
        yield
    finally:
        await client.close()


app = FastAPI(
    title="Explainable Payment Holds",
    description="Synthetic data and PayPal sandbox only.",
    version="0.1.0",
    lifespan=lifespan,
)
install_error_handlers(app)
for _router in (
    api_router,
    ingest_router,
    paypal_router,
    screening_router,
    detect_router,
    decision_router,
    audit_router,
):
    app.include_router(_router)


@app.get("/health", response_model=Health)
async def health() -> Health | JSONResponse:
    graph = getattr(app.state, "graph", None)
    if graph is None:
        return JSONResponse(status_code=503, content={"status": "starting", "graph": "down"})
    try:
        await graph.run_named("ping", {})
    except GraphError:
        return JSONResponse(status_code=503, content={"status": "degraded", "graph": "down"})
    return Health(status="ok", graph="ok")