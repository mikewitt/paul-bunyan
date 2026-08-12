import time
from collections.abc import Sequence
from lumberjack.schema import LogRecordRow
from lumberjack.store import SQLiteRecordStore

def benchmark_append():
    store = SQLiteRecordStore(":memory:")

    row = LogRecordRow(
        logger_name="test",
        level_name="INFO",
        level_no=20,
        msg="test message",
        message="test message",
        pathname="test.py",
        filename="test.py",
        module="test",
        func_name="test_func",
        lineno=1,
        created=time.time(),
        thread=1,
        thread_name="MainThread",
        process=1,
        process_name="MainProcess",
        exc_text=None,
        stack_text=None,
        task_name=None,
        task_id=None,
        parent_task_id=None,
        template_id=None,
    )

    batch = [row] * 10

    # warm up
    for _ in range(100):
        store.append(batch)

    start_time = time.perf_counter()
    for _ in range(50000):
        store.append(batch)
    end_time = time.perf_counter()

    print(f"Elapsed time: {end_time - start_time:.4f} seconds")

if __name__ == "__main__":
    benchmark_append()
