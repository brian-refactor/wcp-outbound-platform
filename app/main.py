from fastapi import FastAPI

from app.routers import prospects, webhooks

app = FastAPI(
    title="WCP Outbound Platform",
    description="Investor acquisition and lead generation platform",
    version="0.1.0",
)

app.include_router(prospects.router)
app.include_router(webhooks.router)


@app.get("/health")
def health():
    return {"status": "ok"}
