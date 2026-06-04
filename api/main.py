import os
import random
import time
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


SERVER_NAME = os.getenv("SERVER_NAME", "server")
START_TIME = time.time()

REQUEST_COUNT = Counter(
    "api_http_requests_total",
    "Total HTTP requests handled by the API.",
    ["server", "method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "api_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["server", "method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
UPTIME = Gauge(
    "api_uptime_seconds",
    "Seconds since the API container started.",
    ["server"],
)

app = FastAPI(title=f"{SERVER_NAME} API")


@app.middleware("http")
async def record_metrics(request: Request, call_next: Callable) -> Response:
    if request.url.path == "/metrics":
        return await call_next(request)

    start = time.perf_counter()
    status = "500"

    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        elapsed = time.perf_counter() - start
        path = request.scope.get("route").path if request.scope.get("route") else request.url.path
        REQUEST_COUNT.labels(SERVER_NAME, request.method, path, status).inc()
        REQUEST_LATENCY.labels(SERVER_NAME, request.method, path).observe(elapsed)


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
    UPTIME.labels(SERVER_NAME).set(time.time() - START_TIME)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
