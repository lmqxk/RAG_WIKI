"""保留的独立 Worker 入口。

MVP 默认由 API 进程中的持久化任务管理器执行解析；后续可在不改变任务表的情况下拆分进程。
"""

from .main import run

if __name__ == "__main__":
    run()
