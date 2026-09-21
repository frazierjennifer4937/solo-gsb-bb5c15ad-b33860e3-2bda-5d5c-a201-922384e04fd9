"""FastAPI application: create tidal navigation-window tasks and query them."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import sqlite3
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from app import db
from app.config import settings
from app.schemas import ResultOut, TaskOut, WindowRequest
from app.services.tides import compute_navigation_windows


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_task(task_id: str, req: WindowRequest) -> None:
    """Background worker: compute the result and persist status/result."""
    conn = db.get_conn()
    try:
        try:
            result = compute_navigation_windows(
                sample_times=[s.time for s in req.samples],
                sample_heights=[s.height_m for s in req.samples],
                query_start=req.query_start,
                query_end=req.query_end,
                draft_m=req.draft_m,
                clearance_m=req.clearance_m,
                max_gap_seconds=req.max_gap_seconds,
                transit_duration_seconds=req.transit_duration_seconds,
                maintenance_blocks=(
                    [(b.start, b.end) for b in req.maintenance_blocks]
                    if req.maintenance_blocks
                    else None
                ),
            )
            result_out = ResultOut(
                threshold_m=result.threshold_m,
                max_gap_seconds=result.max_gap_seconds,
                windows=[vars(w) for w in result.windows],
                unknown=[vars(u) for u in result.unknown],
                departure_windows=(
                    [vars(d) for d in result.departure_windows]
                    if result.departure_windows is not None
                    else None
                ),
            )
            conn.execute(
                "UPDATE tasks SET status = 'done', result = ? WHERE task_id = ?",
                (result_out.model_dump_json(), task_id),
            )
        except Exception as exc:  # stored so the client sees the failure
            conn.execute(
                "UPDATE tasks SET status = 'failed', error = ? WHERE task_id = ?",
                (f"{type(exc).__name__}: {exc}", task_id),
            )
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # Dependencies installed at image build time; schema initialised here.
    db.init_db()
    yield


app = FastAPI(
    title=settings.api_title,
    version="1.0.0",
    description="基于 PCHIP 插值的潮位安全通航窗口与出发窗口计算（FastAPI + SciPy + SQLite）",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["系统"], summary="健康检查")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post(
    "/tasks",
    response_model=TaskOut,
    status_code=201,
    tags=["任务"],
    summary="创建通航窗口计算任务",
)
def create_task(
    req: WindowRequest,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_db),
) -> TaskOut:
    task_id = uuid.uuid4().hex
    created_at = _now()
    conn.execute(
        "INSERT INTO tasks (task_id, status, request, result, error, created_at) "
        "VALUES (?, 'pending', ?, NULL, NULL, ?)",
        (task_id, req.model_dump_json(), created_at.isoformat()),
    )
    conn.commit()
    background_tasks.add_task(_run_task, task_id, req)
    return TaskOut(
        task_id=task_id,
        status="pending",
        created_at=created_at,
        request=req,
        result=None,
        error=None,
    )


@app.get(
    "/tasks/{task_id}",
    response_model=TaskOut,
    tags=["任务"],
    summary="按 ID 查询任务及通航窗口结果",
)
def get_task(
    task_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> TaskOut:
    row = conn.execute(
        "SELECT task_id, status, request, result, error, created_at "
        "FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    created_at = row["created_at"]
    if created_at.endswith("Z"):
        created_at = created_at[:-1] + "+00:00"

    return TaskOut(
        task_id=row["task_id"],
        status=row["status"],
        created_at=datetime.fromisoformat(created_at),
        request=WindowRequest.model_validate_json(row["request"]),
        result=(
            ResultOut.model_validate_json(row["result"])
            if row["result"]
            else None
        ),
        error=row["error"],
    )
