"""
UI and Learnings API routes — mounted into the main FastAPI app.
"""
import os
import logging
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_INDEX_HTML = os.path.join(_STATIC_DIR, "index.html")


# ── Request models ────────────────────────────────────────────────────────────

class SetContentRequest(BaseModel):
    content: str


class AppendFactRequest(BaseModel):
    fact: str


class ForgetKeywordRequest(BaseModel):
    keyword: str


# ── Router factory ────────────────────────────────────────────────────────────

def create_ui_router(state: dict) -> APIRouter:
    """
    Create and return an APIRouter containing:
      - Learnings CRUD endpoints  (/api/learnings/...)
      - UI static file serving    (/ui/...)
    """
    router = APIRouter()

    def _lm():
        lm = state.get("learnings")
        if lm is None:
            raise HTTPException(status_code=503, detail="Learnings manager not initialized")
        return lm

    # ── Learnings API ─────────────────────────────────────────────────────────

    @router.get("/api/learnings")
    async def list_learnings():
        lm = _lm()
        return {"categories": lm.list_categories()}

    @router.get("/api/learnings/{category}")
    async def get_learnings(category: str):
        lm = _lm()
        content = lm.get(category)
        return {"category": category, "content": content}

    @router.post("/api/learnings/{category}")
    async def create_learnings(category: str):
        lm = _lm()
        try:
            lm.create(category)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "category": category}

    @router.put("/api/learnings/{category}")
    async def set_learnings(category: str, body: SetContentRequest):
        lm = _lm()
        lm.set(category, body.content)
        return {"ok": True, "category": category}

    @router.post("/api/learnings/{category}/append")
    async def append_learning(category: str, body: AppendFactRequest):
        lm = _lm()
        lm.append(category, body.fact)
        return {"ok": True, "category": category, "fact": body.fact}

    @router.delete("/api/learnings/{category}/fact")
    async def forget_learning(category: str, body: ForgetKeywordRequest):
        lm = _lm()
        removed = lm.forget(category, body.keyword)
        return {"ok": True, "category": category, "keyword": body.keyword, "removed": removed}

    # ── UI ────────────────────────────────────────────────────────────────────

    def _serve_index():
        try:
            with open(_INDEX_HTML, "r", encoding="utf-8") as f:
                html = f.read()
            return HTMLResponse(content=html)
        except Exception as e:
            log.error(f"Failed to serve index.html from {_INDEX_HTML}: {type(e).__name__}: {e}")
            raise HTTPException(status_code=500, detail=f"UI error: {type(e).__name__}: {e}")

    @router.get("/ui", include_in_schema=False)
    async def ui_root():
        return _serve_index()

    @router.get("/ui/{path:path}", include_in_schema=False)
    async def ui_catch_all(path: str):
        return _serve_index()

    return router
