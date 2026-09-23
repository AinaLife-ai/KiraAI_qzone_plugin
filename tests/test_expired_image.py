"""需求 3 验收：过期图片不再重复下载/刷屏，能续命的救回来，且不阻塞钩子。"""
import asyncio
import time

import _bootstrap as B

main = B.main
utils = B.qzone_utils
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
SID = "qq:gm:123"


def url_entry(url="https://cdn.qq.com/a.jpg?rkey=old", msg_id=None, age=0):
    return {"source": "url", "url": url, "sender": "A",
            "time": int(time.time()) - age, "desc": None, "msg_id": msg_id}


class TestNegativeCache(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()
        utils.reset_fail_log()

    def test_failed_image_is_not_retried_within_ttl(self):
        calls = []

        async def always_400(url, **kw):
            calls.append(url)
            return B.fetch_result(False, status=400, reason="HTTP 400")

        B.patch_fetch_bytes(always_400)
        entry = url_entry()
        self.kick_describe(self.plugin, entry)
        self.run_(asyncio.sleep(0.2))
        self.assertEqual(len(calls), 1, "首次应尝试一次")
        # 反复调度（模拟每轮 llm_request）不应该再触发下载
        for _ in range(5):
            self.kick_describe(self.plugin, entry)
        self.run_(asyncio.sleep(0.2))
        self.assertEqual(len(calls), 1, "负缓存生效后不应重复下载")
        self.assertTrue(self.plugin._is_desc_failed(self.plugin._entry_key(entry)))

    def test_negative_cache_expires(self):
        self.plugin._desc_failed["url:https://x/a.jpg"] = (time.time() - 9999, 10.0)
        self.assertFalse(self.plugin._is_desc_failed("url:https://x/a.jpg"))

    def test_hook_stays_fast_with_all_expired(self):
        async def always_400(url, **kw):
            await asyncio.sleep(3)
            return B.fetch_result(False, status=400, reason="HTTP 400")

        B.patch_fetch_bytes(always_400)
        self.plugin._image_registry[SID] = [url_entry(f"https://x/{i}.jpg") for i in range(5)]
        marks = {}

        async def call_hook():
            await self.plugin._inject_image_manifest(
                B.KiraMessageBatchEvent(sid=SID, messages=[], session=None), B.LLMRequest(), None)
            marks['end'] = time.monotonic()

        start = time.monotonic()
        self.run_(call_hook())
        self.assertLess(marks['end'] - start, 0.3)

    def test_vlm_unavailable_uses_short_cooldown(self):
        self.ctx.provider_mgr = None  # 拿不到默认 VLM
        B.patch_fetch_bytes(lambda *a, **k: asyncio.sleep(0, result=B.fetch_result(True, data=JPEG)))
        entry = url_entry()
        self.kick_describe(self.plugin, entry)
        self.run_(asyncio.sleep(0.2))
        item = self.plugin._desc_failed.get(self.plugin._entry_key(entry))
        self.assertIsNotNone(item)
        self.assertEqual(item[1], main.VLM_UNAVAILABLE_TTL)


class TestRenewal(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        utils.reset_fail_log()

    def test_get_msg_renewal_saves_the_image(self):
        """链接过期但可续命：换新签名 URL 后必须能拿到描述，且不落负缓存。"""
        ada = B.FakeAdapter('qq_ada', send_result={
            'status': 'ok',
            'data': {'message': [{'type': 'image', 'data': {'url': 'https://cdn.qq.com/a.jpg?rkey=new'}}]},
        })
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': ada})
        seen = []

        async def fetch(url, **kw):
            seen.append(url)
            if 'rkey=new' in url:
                return B.fetch_result(True, data=JPEG)
            return B.fetch_result(False, status=400, reason="HTTP 400")

        async def fake_desc(**kw):
            return "救回来了"

        B.patch_fetch_bytes(fetch)
        B.patch_desc_img(fake_desc)
        entry = url_entry(msg_id=555)
        self.kick_describe(self.plugin, entry)
        self.run_(asyncio.sleep(0.3))
        self.assertEqual(entry.get("desc"), "救回来了")
        self.assertEqual(seen[-1], "https://cdn.qq.com/a.jpg?rkey=new")
        self.assertFalse(self.plugin._is_desc_failed(self.plugin._entry_key(entry)))

    def test_renewal_keeps_content_fingerprint(self):
        """换签名不能换掉"这张图是谁"——否则去重身份会变脸。"""
        ada = B.FakeAdapter('qq_ada', send_result={
            'status': 'ok',
            'data': {'message': [{'type': 'image', 'data': {'url': 'https://cdn.qq.com/a.jpg?rkey=new'}}]},
        })
        self.plugin, _ = B.make_plugin(adapters={'qq_ada': ada})
        old_url = "https://cdn.qq.com/a.jpg?rkey=old"
        self.plugin._url_md5[old_url] = "abc123"
        entry = url_entry(old_url, msg_id=555)
        ok = self.run_(self.plugin._refresh_image_url(entry))
        self.assertTrue(ok)
        self.assertEqual(self.plugin._url_md5.get(entry["url"]), "abc123")

    def test_stale_history_image_uses_renewal_first(self):
        """登记时就预判过期的历史图：应先续命，而不是先做一次注定 400 的下载。"""
        old_ts = int(time.time()) - 7200
        ada = B.FakeAdapter('qq_ada', send_result={
            'status': 'ok',
            'data': {'messages': [{
                'message_id': 900, 'time': old_ts,
                'sender': {'nickname': 'A', 'user_id': 1},
                'message': [{'type': 'image', 'data': {'url': 'https://cdn.qq.com/old.jpg?rkey=x'}}],
            }]},
        })
        self.plugin, _ = B.make_plugin(adapters={'qq_ada': ada})
        calls = []

        async def fetch(url, **kw):
            calls.append(url)
            return B.fetch_result(True, data=JPEG)

        B.patch_fetch_bytes(fetch)
        self.run_(self.plugin._fetch_history_messages("group", "123", 20))
        entries = self.plugin._image_registry[SID]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].get("stale"), "超过 rkey 有效期的历史图应被预判为可能过期")


class TestDeliverableImageTool(B.LoopTestCase):
    def test_describe_image_tool_uses_bounded_timeout(self):
        """qzone_describe_image（用户/AI 主动调用）也要用统一超时，避免 60s 默认值。"""
        import inspect
        src = inspect.getsource(main.QzonePlugin.tool_describe_image)
        self.assertIn('fetch_bytes', src)
        self.assertIn('image_download_timeout', src)
