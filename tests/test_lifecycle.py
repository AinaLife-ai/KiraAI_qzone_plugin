"""启动与卸载冒烟：插件能正常建立会话、能干净地停止（不残留后台任务）。"""
import asyncio
import json

import _bootstrap as B

COOKIES = "uin=o10001; skey=abc; p_skey=def; p_uin=o10001"


class TestStartup(B.LoopTestCase):
    def test_initialize_builds_session_from_onebot_cookie(self):
        ada = B.FakeAdapter('qq_ada', send_result={'status': 'ok', 'data': {'cookies': COOKIES}})
        plugin, ctx = B.make_plugin(adapters={'qq_ada': ada})
        self.run_(plugin.initialize())
        self.assertEqual(plugin.my_uin, 10001)
        self.assertIsNotNone(plugin.api)
        self.assertFalse(plugin._init_failed)
        self.run_(plugin.terminate())

    def test_initialize_without_cookie_schedules_retry(self):
        ada = B.FakeAdapter('qq_ada', error=TimeoutError('请求 get_cookies 超时'))
        plugin, ctx = B.make_plugin(adapters={'qq_ada': ada})
        self.run_(plugin.initialize())
        self.assertIsNotNone(plugin._startup_retry_task, "应安排延迟自动重试而不是直接判死")
        self.run_(plugin.terminate())

    def test_initialize_with_manual_cookie_only(self):
        plugin, ctx = B.make_plugin(cfg={'auto_refresh_cookie': False, 'cookies_str': COOKIES})
        self.run_(plugin.initialize())
        self.assertEqual(plugin.my_uin, 10001)
        self.run_(plugin.terminate())

    def test_terminate_cancels_background_tasks_and_flushes_state(self):
        ada = B.FakeAdapter('qq_ada', send_result={'status': 'ok', 'data': {'cookies': COOKIES}})
        plugin, ctx = B.make_plugin(adapters={'qq_ada': ada})

        async def slow_fetch(url, **kw):
            await asyncio.sleep(30)
            return B.fetch_result(False, reason='不该跑到这里')

        B.patch_fetch_bytes(slow_fetch)
        self.run_(plugin.initialize())
        # 制造一个后台识图任务和一个待落盘的状态变更
        entry = {"source": "url", "url": "https://x/slow.jpg", "sender": "A",
                 "time": 1, "desc": None, "msg_id": None}
        self.kick_describe(plugin, entry)
        self.run_(asyncio.sleep(0.1))
        remaining = [t for t in plugin._bg_tasks if not t.done()]
        self.assertTrue(remaining, "应存在进行中的后台任务")

        self.run_(plugin.terminate())
        self.assertEqual(len(plugin._bg_tasks), 0, "卸载后不应残留后台任务")
        self.assertIsNone(plugin.api)
        state = json.loads((ctx._data_dir / 'state.json').read_text(encoding='utf-8'))
        self.assertIn('published_image_history', state)

    def test_initialize_survives_broken_scheduler(self):
        """宿主时区异常导致调度器构造失败时，插件仍应可用（只是没有定时任务）。"""
        plugin, ctx = B.make_plugin()
        plugin.scheduler = None
        self.run_(plugin._setup_scheduled_jobs())   # 不应抛异常
        self.run_(plugin.terminate())
