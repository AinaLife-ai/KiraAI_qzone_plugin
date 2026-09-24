"""审查中发现的 5 处逻辑漏洞的回归守卫。"""
import asyncio
import time

import _bootstrap as B

main = B.main
SID = "qq:gm:123"


class FakeApi:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class TestSelfHealRearm(B.LoopTestCase):
    def test_self_heal_fires_again_every_threshold(self):
        """自愈不能只尝试一次：失败后每再失败 threshold 次必须重新触发。"""
        ada = B.FakeAdapter('qq_ada', error=TimeoutError('请求 get_cookies 超时'))
        plugin, ctx = B.make_plugin(
            adapters={'qq_ada': ada},
            cfg={'cookie_self_heal': True, 'cookie_self_heal_threshold': 2},
        )
        triggers = []
        plugin._trigger_self_heal = lambda reason: triggers.append(reason)

        for _ in range(6):
            with self.assertRaises(Exception):
                self.run_(plugin._call_onebot_action('get_cookies', {}))
        self.assertEqual(len(triggers), 3, f"阈值 2、失败 6 次应触发 3 次，实际 {len(triggers)}")


class TestSoftResetDoesNotBreakInflight(B.LoopTestCase):
    def test_api_not_closed_synchronously(self):
        plugin, ctx = B.make_plugin()
        api = FakeApi()
        plugin.api = api
        plugin.session = object()
        self.run_(plugin._soft_reset_connection('测试'))
        self.assertFalse(api.closed, "软重置不应当场关闭可能在用的会话")
        self.assertIsNone(plugin.api)
        self.assertIn(api, plugin._detached_apis)

    def test_detached_api_closed_on_terminate(self):
        plugin, ctx = B.make_plugin()
        api = FakeApi()
        plugin.api = api
        self.run_(plugin._soft_reset_connection('测试'))
        self.run_(plugin.terminate())
        self.assertTrue(api.closed, "卸载时应兜底关闭被摘掉的旧会话")
        self.assertEqual(plugin._detached_apis, [])

    def test_delayed_close_eventually_closes(self):
        plugin, ctx = B.make_plugin()
        api = FakeApi()
        plugin.api = api
        self.run_(plugin._soft_reset_connection('测试'))
        # 直接调用延迟关闭（把等待改成 0）验证它真的会关
        self.run_(plugin._close_api_later(api, delay=0))
        self.assertTrue(api.closed)
        self.assertNotIn(api, plugin._detached_apis)


class TestReloadUnlock(B.LoopTestCase):
    def test_reload_failure_does_not_lock_forever(self):
        plugin, ctx = B.make_plugin()

        async def boom(plugin_id):
            raise RuntimeError('重载失败')

        ctx.plugin_mgr.reload = boom
        plugin._last_auto_reload_ts = 0.0

        async def trigger():
            plugin._schedule_hard_reload('测试')
            await asyncio.sleep(0.1)

        self.run_(trigger())
        self.assertFalse(plugin._reload_inflight, "重载失败后必须解锁，否则后续重试被永久锁死")


class TestManifestRobustness(B.LoopTestCase):
    """清单相关：钩子只读 + 脏值容错（只列框架已描述的图片）。"""

    def _entries(self, times):
        return [
            {"source": "url", "url": f"https://x/{i}.jpg", "sender": "A",
             "time": t, "desc": "框架给的描述", "msg_id": None}
            for i, t in enumerate(times)
        ]

    def test_bad_time_does_not_kill_manifest(self):
        plugin, ctx = B.make_plugin()
        plugin._image_registry[SID] = self._entries([int(time.time()), "abc", None])
        req = B.LLMRequest()
        self.run_(plugin._inject_image_manifest(
            B.KiraMessageBatchEvent(sid=SID, messages=[], session=None), req, None))
        self.assertEqual(len(req.user_prompt), 1, "单条时间脏值不能让整份清单消失")
        self.assertIn("框架给的描述", req.user_prompt[0].text)
        self.assertIn("时间未知", req.user_prompt[0].text)

    def test_hook_never_reads_database(self):
        """钩子只读内存里的描述：连共享缓存都不查（因此不可能阻塞）。"""
        plugin, ctx = B.make_plugin()
        calls = []

        async def counting_db(md5):
            calls.append(md5)
            return None

        ctx.db.get_image_desc_cache = counting_db
        plugin._image_registry[SID] = self._entries([int(time.time())])
        self.run_(plugin._inject_image_manifest(
            B.KiraMessageBatchEvent(sid=SID, messages=[], session=None), B.LLMRequest(), None))
        self.assertEqual(calls, [], "钩子里不应发生任何数据库查询")

    def test_soft_reset_still_works(self):
        plugin, ctx = B.make_plugin()
        self.run_(plugin._soft_reset_connection('测试'))
        self.assertIsNone(plugin.api)
        self.assertEqual(plugin._last_cookie_refresh, 0.0)
