from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.routers import prospects, webhooks
from app.routers import stats, dashboard

app = FastAPI(
    title="WCP Outbound Platform",
    description="Investor acquisition and lead generation platform",
    version="0.1.0",
)

class DashboardAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login for all /dashboard/* paths."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        is_protected = path == "/dashboard" or path.startswith("/dashboard/")
        is_authenticated = request.session.get("authenticated", False)
        # Dev bypass: if no password is configured, skip auth
        auth_enabled = bool(settings.dashboard_password)
        if is_protected and auth_enabled and not is_authenticated:
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)


# add_middleware stacks in reverse: last added = outermost = runs first.
# SessionMiddleware must be outermost so the session is ready when
# DashboardAuthMiddleware runs.
app.add_middleware(DashboardAuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=False)

app.include_router(dashboard.auth_router)
app.include_router(prospects.router)
app.include_router(webhooks.router)
app.include_router(stats.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"status": "ok"}
