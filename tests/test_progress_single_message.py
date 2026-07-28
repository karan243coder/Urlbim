import asyncio
import unittest

from helper_funcs import display_progress as dp


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self, message_id, chat_id=123):
        self.id = message_id
        self.message_id = message_id
        self.chat = FakeChat(chat_id)
        self.deleted = False
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True


class SingleProgressDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        dp.cleanup_all_progress()

    def tearDown(self):
        dp.cleanup_all_progress()

    async def test_two_simultaneous_candidates_keep_exactly_one_message(self):
        first = FakeMessage(1)
        second = FakeMessage(2)

        claimed = await asyncio.gather(
            dp.claim_user_progress_message(123, first),
            dp.claim_user_progress_message(123, second),
        )

        canonical = dp.get_user_message(123)
        self.assertIsNotNone(canonical)
        self.assertTrue(all(item is canonical for item in claimed))
        self.assertEqual(sum(msg.deleted for msg in (first, second)), 1)

    async def test_set_user_message_does_not_replace_live_dashboard(self):
        first = FakeMessage(10)
        second = FakeMessage(11)
        self.assertIs(dp.set_user_message(123, first), first)
        self.assertIs(dp.set_user_message(123, second), first)
        self.assertIs(dp.get_user_message(123), first)

    async def test_mobile_dashboard_contains_pipeline_footer(self):
        dp.register_task("live", 123, "a very long mobile video title.mp4", 100, "download", "yt-dlp")
        dp.update_task("live", 50, 100, 10, "downloading", "yt-dlp")
        text = await dp.build_advanced_progress_text(123)
        self.assertIn("BIMBO LIVE", text)
        self.assertIn("PIPELINE QUEUE", text)
        self.assertIn("KOYEB HEALTH", text)
        self.assertIn("\n", text)
        self.assertNotIn("\\n", text)

    async def test_waiting_task_clears_stale_speed(self):
        dp.register_task("queued", 123, "large.mp4", 100, "download", "yt-dlp")
        dp.update_task("queued", 50, 100, 10_000_000, "downloading", "yt-dlp")
        dp.update_task("queued", 50, 100, 0, "waiting", "yt-dlp")
        task = dp.get_task("queued")
        self.assertEqual(task["speed"], 0)
        self.assertEqual(task["avg_speed"], 0)
        self.assertEqual(task["status"], "waiting")

    async def test_finished_task_keeps_dashboard_until_last_task_finishes(self):
        dashboard = FakeMessage(20)
        await dp.claim_user_progress_message(123, dashboard)
        dp.register_task("one", 123, "one.mp4", 100, "download", "yt-dlp")
        dp.register_task("two", 123, "two.mp4", 100, "download", "yt-dlp")

        dp.remove_task("one")
        deleted = await dp.finalize_user_progress(None, 123, dashboard)
        self.assertFalse(deleted)
        self.assertFalse(dashboard.deleted)
        self.assertIs(dp.get_user_message(123), dashboard)

        dp.remove_task("two")
        deleted = await dp.finalize_user_progress(None, 123, dashboard)
        self.assertTrue(deleted)
        self.assertTrue(dashboard.deleted)
        self.assertIsNone(dp.get_user_message(123))


if __name__ == "__main__":
    unittest.main()
