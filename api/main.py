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

app = FastAPI(title=f"{SERVER_NAME} API")
APP_INFO.labels(SERVER_NAME, APP_VERSION).set(1)


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
        "endpoints": ["/health", "/api/fast", "/api/slow", "/api/error", "/metrics"],
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


@app.get("/metrics")
def metrics() -> Response:
    UPTIME_SECONDS.labels(SERVER_NAME).set(time.time() - START_TIME)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
