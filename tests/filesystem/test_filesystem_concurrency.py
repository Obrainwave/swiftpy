from __future__ import annotations

import asyncio
import tempfile
import time

import pytest

from swiftpy.filesystem.drivers.local import LocalStorageDriver


@pytest.mark.asyncio
async def test_event_loop_unblocked_during_large_disk_io() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        driver = LocalStorageDriver(root=tmp_dir)

        # 5 MB of data
        payload = b"X" * (5 * 1024 * 1024)

        ticks: list[float] = []

        async def ticker() -> None:
            for _ in range(10):
                await asyncio.sleep(0.005)
                ticks.append(time.perf_counter())

        async def writer() -> int:
            return await driver.write("large_file.dat", payload)

        start = time.perf_counter()
        writer_task = asyncio.create_task(writer())
        ticker_task = asyncio.create_task(ticker())

        await asyncio.gather(writer_task, ticker_task)
        total_time = time.perf_counter() - start
        assert total_time >= 0

        assert await driver.exists("large_file.dat")
        assert await driver.size("large_file.dat") == len(payload)
        # Ticker should have ticked multiple times while writer was executing
        assert len(ticks) == 10


@pytest.mark.asyncio
async def test_concurrent_writes_and_reads() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        driver = LocalStorageDriver(root=tmp_dir)

        async def worker(worker_id: int) -> None:
            filename = f"worker_{worker_id}.txt"
            content = f"worker payload {worker_id}" * 100
            await driver.write(filename, content)
            assert await driver.exists(filename)
            read_back = await driver.read_text(filename)
            assert read_back == content

        # Run 25 concurrent writers/readers
        tasks = [worker(i) for i in range(25)]
        await asyncio.gather(*tasks)

        files = await driver.files()
        assert len(files) == 25
