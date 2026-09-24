"""过期图片相关（按"只走免费路径"的新策略重写）。

新的分工：
- 清单/候选：**只查免费路径**（已知描述），绝不下载、绝不识图；
- 显式识图（AI 调 qzone_describe_image / 可选的评论前识图）：仍然会下载 + 调 VLM，
  并保留失败负缓存；
- 发布路径仍然会为历史 URL 做 get_msg 续命（换新签名），这不涉及识图。
"""
import asyncio
import time

import _bootstrap as B

main = B.main
utils = B.qzone_utils
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
SID = "qq:gm:123"
VLM = "一只橘猫"


def url_entry(url="https://cdn.qq.com/a.jpg?rkey=old", msg_id=None, desc=None):
    return {"source": "url", "url": url, "sender": "A",
            "time": int(time.time()), "desc": desc, "msg_id": msg_id}


class TestFreePathOnly(B.LoopTestCase):
    """免费路径：绝不下载、绝不识图。"""

    def setUp(self):
        super().setUp()
        utils.reset_fail_log()
        self.plugin, self.ctx = B.make_plugin()
        self.downloads = []

        async def fake_fetch(url, **kw):
            self.downloads.append(url)
            return B.fetch_result(True, data=JPEG)

        async def fake_desc(**kw):
            raise AssertionError('免费路径不该调用 VLM')

        B.patch_fetch_bytes(fake_fetch)
        B.patch_desc_img(fake_desc)

    def test_unknown_url_returns_empty_without_download(self):
        desc = self.run_(self.plugin._lookup_cached_url_desc("https://x/unknown.jpg"))
        self.assertEqual(desc, "")
        self.assertEqual(self.downloads, [], '查不到描述时不能去下载')

    def test_known_url_uses_registry_caption(self):
        """同一张图被 bot 当消息收到过 → 清单条目上有框架的 caption，直接复用。"""
        url = "https://cdn.qq.com/a.jpg?rkey=1"
        self.plugin._image_registry[SID] = [
            {"elem": None, "url": url, "sender": "A", "time": int(time.time()),
             "desc": "框架的描述", "msg_id": None}
        ]
        # 模拟"该 URL 对应的图片是一个有 caption 的元素"
        elem = B.Image(url)
        elem.caption = "框架的描述"
        self.plugin._image_registry[SID][0]["elem"] = elem
        desc = self.run_(self.plugin._lookup_cached_url_desc(url))
        self.assertEqual(desc, "框架的描述")
        self.assertEqual(self.downloads, [], '应完全不下载')

    def test_known_md5_hits_shared_cache(self):
        url = "https://cdn.qq.com/b.jpg?rkey=2"
        self.plugin._url_md5[url] = "md5-b"
        self.run_(self.ctx.db.add_image_desc_cache("md5-b", "缓存里的描述",
                                                   count=2, last_seen=int(time.time())))
        desc = self.run_(self.plugin._lookup_cached_url_desc(url))
        self.assertEqual(desc, "缓存里的描述")
        self.assertEqual(self.downloads, [], '应完全不下载')

    def test_history_registration_does_not_download_or_describe(self):
        """登记历史图片：只入清单，不下载、不识图。"""
        ada = B.FakeAdapter('qq_ada', send_result={
            'status': 'ok',
            'data': {'messages': [{
                'message_id': 900, 'time': int(time.time()),
                'sender': {'nickname': 'A', 'user_id': 1},
                'message': [{'type': 'image',
                             'data': {'url': 'https://cdn.qq.com/old.jpg?rkey=x'}}],
            }]},
        })
        plugin, _ = B.make_plugin(adapters={'qq_ada': ada})
        self.run_(plugin._fetch_history_messages("group", "123", 20))
        entries = plugin._image_registry[SID]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["desc"])
        self.assertEqual(self.downloads, [], '登记时不该有任何下载')
        self.assertEqual(plugin._bg_tasks, set(), '登记时不该起后台任务')


class TestExplicitDescribe(B.LoopTestCase):
    """显式识图（AI 主动 / 评论前识图）：会下载 + 调 VLM，并有失败负缓存。"""

    def setUp(self):
        super().setUp()
        utils.reset_fail_log()
        self.plugin, self.ctx = B.make_plugin()

    def test_success_is_cached_in_memory(self):
        calls = []

        async def fake_fetch(url, **kw):
            calls.append(url)
            return B.fetch_result(True, data=JPEG)

        async def fake_desc(**kw):
            return VLM

        B.patch_fetch_bytes(fake_fetch)
        B.patch_desc_img(fake_desc)
        url = "https://cdn.qq.com/a.jpg?rkey=1"
        self.assertEqual(self.run_(self.plugin._describe_image_url(url)), VLM)
        self.assertEqual(self.run_(self.plugin._describe_image_url(url)), VLM)
        self.assertEqual(len(calls), 1, '第二次应命中内存缓存，不再下载')

    def test_failure_marks_negative_cache(self):
        calls = []

        async def always_400(url, **kw):
            calls.append(url)
            return B.fetch_result(False, status=400, reason="HTTP 400")

        B.patch_fetch_bytes(always_400)
        url = "https://cdn.qq.com/expired.jpg?rkey=old"
        self.assertEqual(self.run_(self.plugin._describe_image_url(url)), "")
        self.assertEqual(self.run_(self.plugin._describe_image_url(url)), "")
        self.assertEqual(len(calls), 1, '负缓存内不应重复下载')
        self.assertTrue(self.plugin._is_desc_failed(f"url:{utils.clean_url(url)}"))

    def test_negative_cache_expires(self):
        self.plugin._desc_failed["url:https://x/a.jpg"] = (time.time() - 99999, 10.0)
        self.assertFalse(self.plugin._is_desc_failed("url:https://x/a.jpg"))


class TestPublishPathRenewal(B.LoopTestCase):
    """发布路径的 get_msg 续命（与识图无关，必须保留）。"""

    def setUp(self):
        super().setUp()
        utils.reset_fail_log()

    def test_resolve_renews_expired_url(self):
        ada = B.FakeAdapter('qq_ada', send_result={
            'status': 'ok',
            'data': {'message': [{'type': 'image',
                                  'data': {'url': 'https://cdn.qq.com/a.jpg?rkey=new'}}]},
        })
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': ada})
        self.plugin._image_registry[SID] = [url_entry(msg_id=555, desc="已描述")]
        resolved = self.run_(self.plugin._resolve_manifest_images(SID, [1]))
        self.assertEqual(resolved, ["https://cdn.qq.com/a.jpg?rkey=new"])

    def test_renewal_keeps_content_fingerprint(self):
        ada = B.FakeAdapter('qq_ada', send_result={
            'status': 'ok',
            'data': {'message': [{'type': 'image',
                                  'data': {'url': 'https://cdn.qq.com/a.jpg?rkey=new'}}]},
        })
        self.plugin, _ = B.make_plugin(adapters={'qq_ada': ada})
        old = "https://cdn.qq.com/a.jpg?rkey=old"
        self.plugin._url_md5[old] = "abc123"
        entry = url_entry(old, msg_id=555)
        self.assertTrue(self.run_(self.plugin._refresh_image_url(entry)))
        self.assertEqual(self.plugin._url_md5.get(entry["url"]), "abc123")


class TestDescribeTool(B.LoopTestCase):
    def test_tool_uses_bounded_timeout(self):
        import inspect
        src = inspect.getsource(main.QzonePlugin.tool_describe_image)
        self.assertIn('fetch_bytes', src)
        self.assertIn('image_download_timeout', src)
