import os
import random
import time
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


SERVER_NAME = os.getenv("SERVER_NAME", "server")
APP_VERSION = os.getenv("APP_VERSION", "local")
START_TIME = time.time()

APP_INFO = Gauge(
    "api_app_info",
    "Static API application information.",
    ["server", "version"],
)
HTTP_REQUESTS_TOTAL = Counter(
    "api_http_requests_total",
    "Total HTTP requests handled by the API.",
    ["server", "method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "api_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["server", "method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "api_http_requests_in_progress",
    "HTTP requests currently being handled by the API.",
    ["server", "method", "path"],
)
UPTIME_SECONDS = Gauge(
    "api_uptime_seconds",
    "Seconds since the API container started.",
    ["server"],
)
MEMORY_RESERVED_BYTES = Gauge(
    "api_memory_reserved_bytes",
    "Application-managed bytes intentionally retained for memory allocation testing.",
    ["server"],
)
MEMORY_RESERVED_CHUNKS = Gauge(
    "api_memory_reserved_chunks",
    "Number of retained memory chunks for allocation testing.",
    ["server"],
)

app = FastAPI(title=f"{SERVER_NAME} API")
APP_INFO.labels(SERVER_NAME, APP_VERSION).set(1)
MEMORY_STORE: list[bytearray] = []
MAX_RESERVED_BYTES = 96 * 1024 * 1024


def reserved_bytes() -> int:
    return sum(len(chunk) for chunk in MEMORY_STORE)


def update_memory_metrics() -> None:
    MEMORY_RESERVED_BYTES.labels(SERVER_NAME).set(reserved_bytes())
    MEMORY_RESERVED_CHUNKS.labels(SERVER_NAME).set(len(MEMORY_STORE))


def route_path(request: Request) -> str:
    route = request.scope.get("route")
    return route.path if route else request.url.path


@app.middleware("http")
async def record_metrics(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.url.path == "/metrics":
        return await call_next(request)

    path = route_path(request)
    start = time.perf_counter()
    status = "500"
    HTTP_REQUESTS_IN_PROGRESS.labels(SERVER_NAME, request.method, path).inc()

    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        elapsed = time.perf_counter() - start
        HTTP_REQUESTS_TOTAL.labels(SERVER_NAME, request.method, path, status).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(SERVER_NAME, request.method, path).observe(elapsed)
        HTTP_REQUESTS_IN_PROGRESS.labels(SERVER_NAME, request.method, path).dec()


@app.get("/")
def root() -> dict:
    return {
        "server": SERVER_NAME,
        "endpoints": [
            "/health",
            "/api/fast",
            "/api/slow",
            "/api/error",
            "/api/memory/status",
            "/api/memory/allocate?mb=8&chunks=1",
            "/api/memory/release?mb=8",
            "/api/memory/reset",
            "/metrics",
        ],
    }


@app.get("/health")
def health() -> dict:
    return {
        "server": SERVER_NAME,
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME, 2),
    }


@app.get("/api/fast")
def fast_endpoint() -> dict:
    time.sleep(random.uniform(0.005, 0.03))
    return {
        "server": SERVER_NAME,
        "message": "fast response",
    }


@app.get("/api/slow")
def slow_endpoint() -> dict:
    time.sleep(random.uniform(0.2, 1.2))
    return {
        "server": SERVER_NAME,
        "message": "slow response",
    }


@app.get("/api/error")
def error_endpoint() -> dict:
    if random.random() < 0.5:
        raise HTTPException(status_code=500, detail=f"{SERVER_NAME} simulated error")

    return {
        "server": SERVER_NAME,
        "message": "lucky success",
    }


@app.get("/api/memory/status")
def memory_status() -> dict:
    update_memory_metrics()
    return {
        "server": SERVER_NAME,
        "reserved_bytes": reserved_bytes(),
        "reserved_mb": round(reserved_bytes() / (1024 * 1024), 2),
        "chunks": len(MEMORY_STORE),
        "max_reserved_mb": round(MAX_RESERVED_BYTES / (1024 * 1024), 2),
    }


@app.get("/api/memory/allocate")
def allocate_memory(mb: int = 8, chunks: int = 1) -> dict:
    if mb < 1 or mb > 32:
        raise HTTPException(status_code=400, detail="mb must be between 1 and 32")
    if chunks < 1 or chunks > 8:
        raise HTTPException(status_code=400, detail="chunks must be between 1 and 8")

    bytes_per_chunk = mb * 1024 * 1024
    requested_bytes = bytes_per_chunk * chunks
    current_reserved = reserved_bytes()
    if current_reserved + requested_bytes > MAX_RESERVED_BYTES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{SERVER_NAME} refused allocation because it would exceed "
                f"{MAX_RESERVED_BYTES // (1024 * 1024)}MB of retained memory"
            ),
        )

    for _ in range(chunks):
        block = bytearray(bytes_per_chunk)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        MEMORY_STORE.append(block)

    update_memory_metrics()
    return {
        "server": SERVER_NAME,
        "action": "allocate",
        "allocated_mb": mb * chunks,
        "chunks_added": chunks,
        "reserved_mb": round(reserved_bytes() / (1024 * 1024), 2),
    }


@app.get("/api/memory/release")
def release_memory(mb: int = 8) -> dict:
    if mb < 1 or mb > 96:
        raise HTTPException(status_code=400, detail="mb must be between 1 and 96")

    bytes_to_release = mb * 1024 * 1024
    released_bytes = 0
    released_chunks = 0
    while MEMORY_STORE and released_bytes < bytes_to_release:
        chunk = MEMORY_STORE.pop()
        released_bytes += len(chunk)
        released_chunks += 1

    update_memory_metrics()
    return {
        "server": SERVER_NAME,
        "action": "release",
        "released_mb": round(released_bytes / (1024 * 1024), 2),
        "chunks_removed": released_chunks,
        "reserved_mb": round(reserved_bytes() / (1024 * 1024), 2),
    }


@app.get("/api/memory/reset")
def reset_memory() -> dict:
    released_chunks = len(MEMORY_STORE)
    MEMORY_STORE.clear()
    update_memory_metrics()
    return {
        "server": SERVER_NAME,
        "action": "reset",
        "chunks_removed": released_chunks,
        "reserved_mb": 0,
    }


@app.get("/metrics")
def metrics() -> Response:
    UPTIME_SECONDS.labels(SERVER_NAME).set(time.time() - START_TIME)
    update_memory_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
