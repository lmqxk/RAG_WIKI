"""分层压测 RAG 服务：基础 API、检索链路和完整流式问答。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx


@dataclass
class Sample:
    latency: float
    status: int | None
    error: str | None = None
    stages: dict[str, float] = field(default_factory=dict)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)]


async def request_once(
    client: httpx.AsyncClient,
    scenario: str,
    base_url: str,
) -> Sample:
    started = time.perf_counter()
    try:
        if scenario == "health":
            response = await client.get(f"{base_url}/api/health")
        elif scenario == "documents":
            response = await client.get(f"{base_url}/api/documents")
        elif scenario == "chat":
            response = await client.post(
                f"{base_url}/api/chat",
                json={"question": "请概括当前知识库中的主要内容"},
            )
        else:
            stages: dict[str, float] = {}
            async with client.stream(
                "POST",
                f"{base_url}/api/chat/stream",
                json={"question": "请概括当前知识库中的主要内容"},
            ) as response:
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("event") == "meta":
                        stages = {
                            key: float(value)
                            for key, value in event.items()
                            if key.endswith("_ms") and isinstance(value, (int, float))
                        }
                return Sample(
                    latency=time.perf_counter() - started,
                    status=response.status_code,
                    stages=stages,
                )
        return Sample(time.perf_counter() - started, response.status_code)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return Sample(time.perf_counter() - started, None, f"{type(exc).__name__}: {exc}")


async def run_scenario(
    scenario: str,
    base_url: str,
    concurrency: int,
    duration: float,
    timeout: float,
) -> list[Sample]:
    samples: list[Sample] = []
    stop_at = time.perf_counter() + duration
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), limits=limits) as client:

        async def worker() -> None:
            while time.perf_counter() < stop_at:
                sample = await request_once(client, scenario, base_url)
                async with lock:
                    samples.append(sample)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return samples


def report(scenario: str, samples: list[Sample], duration: float, wall: float) -> None:
    latencies = [sample.latency for sample in samples]
    success = sum(sample.status is not None and 200 <= sample.status < 400 for sample in samples)
    statuses = Counter(str(sample.status) if sample.status is not None else "network_error" for sample in samples)
    print(f"\n[{scenario}] requests={len(samples)} success={success} errors={len(samples) - success}")
    print(
        f"load_window={duration:.1f}s wall_time={wall:.1f}s "
        f"offered_qps={len(samples) / duration:.2f} completed_qps={success / wall:.2f}"
    )
    if latencies:
        print(
            "latency_ms="
            f"avg:{statistics.mean(latencies) * 1000:.1f} "
            f"p50:{percentile(latencies, .50) * 1000:.1f} "
            f"p95:{percentile(latencies, .95) * 1000:.1f} "
            f"p99:{percentile(latencies, .99) * 1000:.1f} "
            f"max:{max(latencies) * 1000:.1f}"
        )
    print(f"status={dict(statuses)}")
    stages: dict[str, list[float]] = {}
    for sample in samples:
        for name, value in sample.stages.items():
            stages.setdefault(name, []).append(value)
    for name, values in stages.items():
        avg = statistics.mean(values)
        print(
            f"stage_{name}=avg:{avg:.1f}ms p95:{percentile(values, .95):.1f}ms "
            f"serial_qps:{1000 / avg:.3f}"
        )
    errors = [sample.error for sample in samples if sample.error]
    if errors:
        print(f"first_error={errors[0]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--scenario",
        choices=("all", "health", "documents", "chat", "chat-stream"),
        default="all",
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.concurrency < 1 or args.duration <= 0 or args.timeout <= 0:
        parser.error("concurrency、duration、timeout 必须为正数")
    scenarios = ("health", "documents", "chat-stream") if args.scenario == "all" else (args.scenario,)
    base_url = args.base_url.rstrip("/")
    for scenario in scenarios:
        started = time.perf_counter()
        samples = asyncio.run(
            run_scenario(scenario, base_url, args.concurrency, args.duration, args.timeout)
        )
        report(scenario, samples, args.duration, time.perf_counter() - started)


if __name__ == "__main__":
    main()
