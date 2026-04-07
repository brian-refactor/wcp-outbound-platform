from fastapi import FastAPI

from app.routers import prospects

app = FastAPI(
    title="WCP Outbound Platform",
    description="Investor acquisition and lead generation platform",
    version="0.1.0",
)

app.include_router(prospects.router)


@app.get("/health")
def health():
    return {"status": "ok"}
