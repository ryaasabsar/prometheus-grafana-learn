import os
import random
import signal
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable


TOTAL_DURATION_SECONDS = int(os.getenv("TOTAL_DURATION_SECONDS", "7200"))
REQUEST_INTERVAL_SECONDS = float(os.getenv("REQUEST_INTERVAL_SECONDS", "1.0"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "3.0"))
LOG_EVERY_SECONDS = int(os.getenv("LOG_EVERY_SECONDS", "30"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "12"))
RANDOM_SEED = int(os.getenv("RANDOM_SEED", "42"))
SCENARIO_SCALE = float(os.getenv("SCENARIO_SCALE", "1.0"))

TARGETS = [
    ("server-a", "http://server-a:8000"),
    ("server-b", "http://server-b:8000"),
]
SHUTDOWN_REQUESTED = False


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_seconds: int
    request_range: tuple[int, int]
    target_weights: dict[str, float]
    endpoint_weights: dict[str, float]
    note: str


SCENARIOS = [
    Scenario(
        name="warmup",
        duration_seconds=600,
        request_range=(1, 2),
        target_weights={"server-a": 0.6, "server-b": 0.4},
        endpoint_weights={
            "/": 0.10,
            "/health": 0.20,
            "/metrics": 0.05,
            "/api/fast": 0.45,
            "/api/slow": 0.15,
            "/api/error": 0.10,
            "/api/memory/status": 0.05,
            "/api/memory/allocate?mb=4&chunks=1": 0.03,
            "/api/memory/release?mb=4": 0.02,
        },
        note="Light traffic to make sure everything is alive and scrapes stay clean.",
    ),
    Scenario(
        name="steady-state",
        duration_seconds=1200,
        request_range=(2, 4),
        target_weights={"server-a": 0.65, "server-b": 0.35},
        endpoint_weights={
            "/": 0.05,
            "/health": 0.10,
            "/metrics": 0.02,
            "/api/fast": 0.55,
            "/api/slow": 0.20,
            "/api/error": 0.10,
            "/api/memory/status": 0.04,
            "/api/memory/allocate?mb=4&chunks=1": 0.02,
            "/api/memory/release?mb=4": 0.02,
        },
        note="Normal production-like traffic with mostly fast requests.",
    ),
    Scenario(
        name="slow-spike",
        duration_seconds=900,
        request_range=(3, 5),
        target_weights={"server-a": 0.55, "server-b": 0.45},
        endpoint_weights={
            "/": 0.05,
            "/health": 0.10,
            "/metrics": 0.02,
            "/api/fast": 0.20,
            "/api/slow": 0.50,
            "/api/error": 0.15,
            "/api/memory/status": 0.03,
            "/api/memory/allocate?mb=8&chunks=1": 0.03,
            "/api/memory/release?mb=4": 0.02,
        },
        note="More slow requests to drive p95 latency and in-progress load upward.",
    ),
    Scenario(
        name="error-burst",
        duration_seconds=900,
        request_range=(3, 6),
        target_weights={"server-a": 0.70, "server-b": 0.30},
        endpoint_weights={
            "/": 0.05,
            "/health": 0.05,
            "/metrics": 0.02,
            "/api/fast": 0.20,
            "/api/slow": 0.20,
            "/api/error": 0.50,
            "/api/memory/status": 0.03,
            "/api/memory/allocate?mb=4&chunks=1": 0.03,
            "/api/memory/release?mb=8": 0.02,
        },
        note="Heavy error endpoint pressure to light up error-rate panels and alerts.",
    ),
    Scenario(
        name="server-b-bias",
        duration_seconds=1200,
        request_range=(2, 5),
        target_weights={"server-a": 0.25, "server-b": 0.75},
        endpoint_weights={
            "/": 0.05,
            "/health": 0.10,
            "/metrics": 0.02,
            "/api/fast": 0.45,
            "/api/slow": 0.25,
            "/api/error": 0.15,
            "/api/memory/status": 0.03,
            "/api/memory/allocate?mb=8&chunks=1": 0.05,
            "/api/memory/release?mb=4": 0.02,
        },
        note="Traffic shifts toward server-b so you can compare utilization across servers.",
    ),
    Scenario(
        name="mixed-peak",
        duration_seconds=1200,
        request_range=(4, 7),
        target_weights={"server-a": 0.60, "server-b": 0.40},
        endpoint_weights={
            "/": 0.05,
            "/health": 0.05,
            "/metrics": 0.02,
            "/api/fast": 0.45,
            "/api/slow": 0.30,
            "/api/error": 0.15,
            "/api/memory/status": 0.03,
            "/api/memory/allocate?mb=8&chunks=2": 0.04,
            "/api/memory/release?mb=8": 0.02,
        },
        note="Peak traffic window with a realistic mix of fast, slow, and error calls.",
    ),
    Scenario(
        name="cooldown",
        duration_seconds=1200,
        request_range=(1, 3),
        target_weights={"server-a": 0.50, "server-b": 0.50},
        endpoint_weights={
            "/": 0.10,
            "/health": 0.25,
            "/metrics": 0.05,
            "/api/fast": 0.35,
            "/api/slow": 0.20,
            "/api/error": 0.10,
            "/api/memory/status": 0.07,
            "/api/memory/release?mb=8": 0.05,
            "/api/memory/reset": 0.03,
        },
        note="Traffic tapers off so efficiency panels and idle-resource alerts become obvious.",
    ),
]


def install_signal_handlers() -> None:
    def _mark_shutdown(signum: int, _frame: object) -> None:
        global SHUTDOWN_REQUESTED
        SHUTDOWN_REQUESTED = True
        print(f"Received signal {signum}, finishing the current batch before exiting.")

    signal.signal(signal.SIGINT, _mark_shutdown)
    signal.signal(signal.SIGTERM, _mark_shutdown)


def weighted_choice(weight_map: dict[str, float]) -> str:
    options = list(weight_map)
    weights = [weight_map[option] for option in options]
    return random.choices(options, weights=weights, k=1)[0]


def find_target(server_name: str) -> tuple[str, str]:
    for name, base_url in TARGETS:
        if name == server_name:
            return name, base_url
    raise ValueError(f"Unknown target {server_name}")


def hit(url: str) -> int | str:
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return "ERR"


def execute_batch(
    executor: ThreadPoolExecutor,
    scenario: Scenario,
    batch_size: int,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    totals_by_status: dict[str, int] = {}
    totals_by_target: dict[str, int] = {}
    totals_by_path: dict[str, int] = {}
    futures = []

    for _ in range(batch_size):
        server_name = weighted_choice(scenario.target_weights)
        path = weighted_choice(scenario.endpoint_weights)
        _, base_url = find_target(server_name)
        futures.append((server_name, path, executor.submit(hit, f"{base_url}{path}")))

    for server_name, path, future in futures:
        status = str(future.result())
        totals_by_status[status] = totals_by_status.get(status, 0) + 1
        totals_by_target[server_name] = totals_by_target.get(server_name, 0) + 1
        totals_by_path[path] = totals_by_path.get(path, 0) + 1

    return totals_by_status, totals_by_target, totals_by_path


def merge_counts(destination: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        destination[key] = destination.get(key, 0) + value


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))


def build_timeline(scenarios: Iterable[Scenario]) -> list[tuple[int, int, Scenario]]:
    timeline = []
    current_start = 0
    for scenario in scenarios:
        scaled_duration = max(1, int(round(scenario.duration_seconds * SCENARIO_SCALE)))
        current_end = current_start + scaled_duration
        timeline.append((current_start, current_end, scenario))
        current_start = current_end
    return timeline


def scenario_for_elapsed(elapsed_seconds: int, timeline: list[tuple[int, int, Scenario]]) -> Scenario:
    for start, end, scenario in timeline:
        if start <= elapsed_seconds < end:
            return scenario
    return timeline[-1][2]


def main() -> None:
    random.seed(RANDOM_SEED)
    install_signal_handlers()
    total_script_duration = sum(max(1, int(round(scenario.duration_seconds * SCENARIO_SCALE))) for scenario in SCENARIOS)
    if TOTAL_DURATION_SECONDS != total_script_duration:
        raise ValueError(
            "TOTAL_DURATION_SECONDS does not match the scenario schedule. "
            f"Expected {total_script_duration}, got {TOTAL_DURATION_SECONDS}."
        )

    timeline = build_timeline(SCENARIOS)
    started_at = time.monotonic()
    last_log_time = started_at
    current_scenario_name = ""
    total_requests = 0
    overall_status: dict[str, int] = {}
    overall_targets: dict[str, int] = {}
    overall_paths: dict[str, int] = {}

    print("Starting scheduled load generator")
    print(f"total_duration_seconds={TOTAL_DURATION_SECONDS}")
    for start, end, scenario in timeline:
        print(
            f"scenario={scenario.name} window={start}s-{end}s "
            f"rps_range={scenario.request_range[0]}-{scenario.request_range[1]} note={scenario.note}"
        )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while not SHUTDOWN_REQUESTED:
            elapsed = int(time.monotonic() - started_at)
            if elapsed >= TOTAL_DURATION_SECONDS:
                break

            scenario = scenario_for_elapsed(elapsed, timeline)
            if scenario.name != current_scenario_name:
                current_scenario_name = scenario.name
                print(
                    f"switching_to={scenario.name} "
                    f"elapsed_seconds={elapsed} "
                    f"target_mix={scenario.target_weights} "
                    f"endpoint_mix={scenario.endpoint_weights}"
                )

            cycle_started_at = time.monotonic()
            batch_size = random.randint(*scenario.request_range)
            status_counts, target_counts, path_counts = execute_batch(executor, scenario, batch_size)
            total_requests += batch_size
            merge_counts(overall_status, status_counts)
            merge_counts(overall_targets, target_counts)
            merge_counts(overall_paths, path_counts)

            now = time.monotonic()
            if now - last_log_time >= LOG_EVERY_SECONDS:
                print(
                    f"elapsed_seconds={elapsed} scenario={scenario.name} total_requests={total_requests} "
                    f"status_counts=[{format_counts(overall_status)}] "
                    f"target_counts=[{format_counts(overall_targets)}] "
                    f"path_counts=[{format_counts(overall_paths)}]"
                )
                last_log_time = now

            cycle_duration = time.monotonic() - cycle_started_at
            sleep_time = max(0.0, REQUEST_INTERVAL_SECONDS - cycle_duration)
            time.sleep(sleep_time)

    print("Load generator finished")
    print(f"final_total_requests={total_requests}")
    print(f"final_status_counts=[{format_counts(overall_status)}]")
    print(f"final_target_counts=[{format_counts(overall_targets)}]")
    print(f"final_path_counts=[{format_counts(overall_paths)}]")


if __name__ == "__main__":
    main()
