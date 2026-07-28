import asyncio
import unittest

from plugins.media_pipeline import (
    DOWNLOAD_STAGE, PRIORITY_BULK, PRIORITY_INTERACTIVE,
    FairStageLimiter, begin_interactive_job, clear_bulk_backlog,
    end_interactive_job, get_pipeline_stats, reset_pipeline_runtime,
    set_bulk_backlog,
)


class FairPipelineTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        reset_pipeline_runtime()

    async def test_global_limit_and_site_limit_do_not_block_other_site(self):
        limiter = FairStageLimiter("download", 2, site_limits={"xhamster": 1})
        await limiter.acquire("xh1", 1, "xhamster")

        xh2 = asyncio.create_task(limiter.acquire("xh2", 1, "xhamster"))
        await asyncio.sleep(0)
        ep = asyncio.create_task(limiter.acquire("ep1", 2, "eporner"))
        await asyncio.wait_for(ep, timeout=1)

        snap = limiter.snapshot()
        self.assertEqual(snap["active"], 2)
        self.assertEqual(snap["waiting"], 1)
        self.assertFalse(xh2.done())

        await limiter.release("xh1")
        await asyncio.wait_for(xh2, timeout=1)
        await limiter.release("xh2")
        await limiter.release("ep1")

    async def test_interactive_job_gets_next_slot_and_holds_bulk_barrier(self):
        await DOWNLOAD_STAGE.acquire("bulk1", 1, "media", priority=PRIORITY_BULK)
        await DOWNLOAD_STAGE.acquire("bulk2", 2, "media", priority=PRIORITY_BULK)
        bulk3 = asyncio.create_task(
            DOWNLOAD_STAGE.acquire("bulk3", 3, "media", priority=PRIORITY_BULK)
        )
        await asyncio.sleep(0)

        await begin_interactive_job("single")
        single = asyncio.create_task(
            DOWNLOAD_STAGE.acquire("single", 4, "media", priority=PRIORITY_INTERACTIVE)
        )
        await asyncio.sleep(0)
        await DOWNLOAD_STAGE.release("bulk1")
        await asyncio.wait_for(single, timeout=1)
        self.assertFalse(bulk3.done())

        await DOWNLOAD_STAGE.release("single")
        await asyncio.sleep(0)
        self.assertFalse(bulk3.done())
        await end_interactive_job("single")
        await asyncio.wait_for(bulk3, timeout=1)
        await DOWNLOAD_STAGE.release("bulk2")
        await DOWNLOAD_STAGE.release("bulk3")

    async def test_round_robin_is_fifo_inside_each_user(self):
        limiter = FairStageLimiter("download", 1)
        await limiter.acquire("active", 99, "media")
        u1_first = asyncio.create_task(limiter.acquire("u1a", 1, "media"))
        u1_second = asyncio.create_task(limiter.acquire("u1b", 1, "media"))
        u2_first = asyncio.create_task(limiter.acquire("u2a", 2, "media"))
        await asyncio.sleep(0)

        await limiter.release("active")
        await asyncio.wait_for(u1_first, timeout=1)
        self.assertFalse(u2_first.done())
        self.assertFalse(u1_second.done())

        await limiter.release("u1a")
        await asyncio.wait_for(u2_first, timeout=1)
        self.assertFalse(u1_second.done())

        await limiter.release("u2a")
        await asyncio.wait_for(u1_second, timeout=1)
        await limiter.release("u1b")

    async def test_bulk_backlog_is_visible_per_user_and_global(self):
        set_bulk_backlog("xh-job", 10, 25, "xhamster", "Channel")
        set_bulk_backlog("ep-job", 20, 7, "eporner", "Profile")
        self.assertEqual(get_pipeline_stats()["bulk_pending"], 32)
        self.assertEqual(get_pipeline_stats(10)["bulk_pending"], 25)
        self.assertEqual(get_pipeline_stats(20)["total_pending"], 7)
        clear_bulk_backlog("xh-job")
        self.assertEqual(get_pipeline_stats()["bulk_pending"], 7)


if __name__ == "__main__":
    unittest.main()
