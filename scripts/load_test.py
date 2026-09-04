"""RAG API 并发压测工具：统计吞吐量、延迟分位数、状态码和错误率。"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass

import httpx


@dataclass
class Result:
    latency: float
    status: int | None
    error: str | None = None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)
    return ordered[index]


async def run_load(
    url: str,
    payload: dict[str, object] | None,
    concurrency: int,
    duration: float,
    timeout: float,
) -> list[Result]:
    results: list[Result] = []
    stop_at = time.perf_counter() + duration
    lock = asyncio.Lock()
    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )
    request_timeout = httpx.Timeout(timeout)

    async with httpx.AsyncClient(limits=limits, timeout=request_timeout) as client:

        async def worker() -> None:
            while time.perf_counter() < stop_at:
                started = time.perf_counter()
                try:
                    if payload is None:
                        response = await client.get(url)
                    else:
                        response = await client.post(url, json=payload)
                    result = Result(
                        latency=time.perf_counter() - started,
                        status=response.status_code,
                    )
                except (httpx.HTTPError, OSError) as exc:  # 将网络错误纳入统计
                    result = Result(
                        latency=time.perf_counter() - started,
                        status=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                async with lock:
                    results.append(result)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return results


def print_report(
    results: list[Result], duration: float, wall_time: float, concurrency: int, url: str
) -> None:
    latencies = [item.latency for item in results]
    success = sum(item.status is not None and 200 <= item.status < 400 for item in results)
    failures = len(results) - success
    statuses = Counter(str(item.status) if item.status is not None else "network_error" for item in results)

    print(f"URL: {url}")
    print(f"Concurrency: {concurrency}")
    print(f"Load window: {duration:.1f}s")
    print(f"Wall time: {wall_time:.1f}s")
    print(f"Requests: {len(results)}")
    print(f"Success: {success} ({success / len(results) * 100:.2f}%)" if results else "Success: 0")
    print(f"Failures: {failures} ({failures / len(results) * 100:.2f}%)" if results else "Failures: 0")
    print(f"Throughput: {len(results) / duration:.2f} req/s" if duration > 0 else "Throughput: n/a")
    if latencies:
        print(
            "Latency: "
            f"avg={statistics.mean(latencies) * 1000:.1f}ms "
            f"p50={percentile(latencies, 0.50) * 1000:.1f}ms "
            f"p95={percentile(latencies, 0.95) * 1000:.1f}ms "
            f"p99={percentile(latencies, 0.99) * 1000:.1f}ms "
            f"max={max(latencies) * 1000:.1f}ms"
        )
    print(f"Status: {dict(statuses)}")
    errors = [item.error for item in results if item.error]
    if errors:
        print(f"First error: {errors[0]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG API concurrent load test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--scenario", choices=("health", "chat"), default="health")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--question", default="请概括当前知识库中的主要内容")
    args = parser.parse_args()
    if args.concurrency < 1 or args.duration <= 0 or args.timeout <= 0:
        parser.error("concurrency、duration、timeout 必须为正数")
    return args


def main() -> None:
    args = parse_args()
    if args.scenario == "health":
        url = f"{args.base_url.rstrip('/')}/api/health"
        payload = None
    else:
        url = f"{args.base_url.rstrip('/')}/api/chat"
        payload = {"question": args.question}

    started = time.perf_counter()
    results = asyncio.run(
        run_load(url, payload, args.concurrency, args.duration, args.timeout)
    )
    elapsed = time.perf_counter() - started
    print_report(results, args.duration, elapsed, args.concurrency, url)


if __name__ == "__main__":
    main()
#.\backend\.venv\Scripts\python.exe scripts\load_test.py `  --base-url http://127.0.0.1:8000 `  --scenario chat `  --concurrency 1 `  --duration 30 `  --timeout 120
