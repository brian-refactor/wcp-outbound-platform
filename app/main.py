from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import prospects, webhooks
from app.routers import stats, dashboard

app = FastAPI(
    title="WCP Outbound Platform",
    description="Investor acquisition and lead generation platform",
    version="0.1.0",
)

app.include_router(prospects.router)
app.include_router(webhooks.router)
app.include_router(stats.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"status": "ok"}
