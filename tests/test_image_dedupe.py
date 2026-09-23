"""图片去重验收。

现场症状：bot 连续几条说说配同一张图。旧实现的三处根因：
1. 去重身份依赖 _url_md5（只有"识过图"才有），URL 换签名后身份就变了；
2. 去重历史与图片清单共用 20 条上限，一条说说配 3 张图时几小时就挤没了；
3. 去重只在"后台直接生成模式"生效，**群聊指令模式（推荐模式）完全没做去重**。
"""
import asyncio
import time

import _bootstrap as B

main = B.main
SID = "qq:gm:123"


def url_entry(url, msg_id=None):
    return {"source": "url", "url": url, "sender": "A",
            "time": int(time.time()), "desc": "已识别", "msg_id": msg_id}


class TestDedupeHistory(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()

    def test_history_not_truncated_to_20(self):
        pairs = [(f"https://x/{i}.jpg", f"md5{i}") for i in range(30)]
        self.plugin._record_published_images([p[0] for p in pairs], md5_pairs=pairs)
        self.assertGreaterEqual(len(self.plugin._published_image_history), 30,
                                "去重历史不能只有 20 条（3 天窗口内会不够用）")

    def test_identity_is_content_based(self):
        """URL 换签名（rkey 刷新）后仍必须认得是同一张图。"""
        self.plugin._record_published_images([], md5_pairs=[("https://x/a.jpg?rkey=old", "abc")])
        self.assertTrue(self.plugin._is_recently_published_image("https://x/a.jpg?rkey=old"))
        self.plugin._url_md5["https://x/a.jpg?rkey=new"] = "abc"
        self.assertTrue(self.plugin._is_recently_published_image("https://x/a.jpg?rkey=new"),
                        "同一张图换了签名之后也必须算已发布过")

    def test_source_fallback_when_no_hash(self):
        self.plugin._record_published_images(["https://x/b.jpg"])
        self.assertTrue(self.plugin._is_recently_published_image("https://x/b.jpg"))
        self.assertFalse(self.plugin._is_recently_published_image("https://x/c.jpg"))

    def test_only_successfully_uploaded_are_recorded(self):
        """部分图片被降级丢弃时：只记录真正发出去的那张。"""
        self.plugin._record_published_images(
            ["https://x/ok.jpg", "https://x/dropped.jpg"],
            md5_pairs=[("https://x/ok.jpg", "m1")],
        )
        self.assertTrue(self.plugin._is_recently_published_image("https://x/ok.jpg"))
        self.assertFalse(self.plugin._is_recently_published_image("https://x/dropped.jpg"),
                         "被丢弃的图不应进入去重历史")

    def test_old_records_pruned(self):
        now = time.time()
        self.plugin._published_image_history = [
            {"identity": "md5:old", "source": "u1", "time": now - 10 * 86400},
            {"identity": "md5:new", "source": "u2", "time": now - 3600},
        ]
        kept = self.plugin._prune_dedupe_history()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["identity"], "md5:new")

    def test_publish_records_api_reported_md5(self):
        resp = B._Simple(ok=True, message=None, data={
            "tid": "t1",
            "image_md5s": [("https://x/a.jpg", "hashA")],
        })

        class FakeApi:
            async def publish(self, post, allow_image_drop=False):
                return resp

        self.plugin.api = FakeApi()

        async def noop():
            return None

        self.plugin._ensure_api = noop
        self.run_(self.plugin._publish("文案", ["https://x/a.jpg"]))
        identities = [item["identity"] for item in self.plugin._published_image_history]
        self.assertIn("md5:hashA", identities)


class TestManifestDedupe(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()
        self.plugin._image_registry[SID] = [
            url_entry("https://x/1.jpg"), url_entry("https://x/2.jpg"), url_entry("https://x/3.jpg"),
        ]

    def test_filter_only_when_requested(self):
        self.plugin._record_published_images([], md5_pairs=[("https://x/1.jpg", "m1")])
        self.assertEqual(len(self.plugin._manifest_entries(SID, apply_dedupe=False)), 3)
        filtered = self.plugin._manifest_entries(SID, apply_dedupe=True)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["url"], "https://x/2.jpg")

    def test_numbering_consistent_between_inject_and_resolve(self):
        """注入的序号与解析用的序号必须是同一份（过滤后）清单，否则会配错图。"""
        self.plugin._record_published_images([], md5_pairs=[("https://x/1.jpg", "m1")])
        resolved = self.run_(self.plugin._resolve_manifest_images(SID, [1], apply_dedupe=True))
        self.assertEqual(resolved, ["https://x/2.jpg"], "序号 1 应指向过滤后的第一张")

    def test_user_explicit_index_not_deduped(self):
        self.plugin._record_published_images([], md5_pairs=[("https://x/1.jpg", "m1")])
        resolved = self.run_(self.plugin._resolve_manifest_images(SID, [1], apply_dedupe=False))
        self.assertEqual(resolved, ["https://x/1.jpg"], "用户主动指定序号时不应被去重拦截")

    def test_hook_excludes_recently_published_in_publish_task(self):
        self.plugin._record_published_images([], md5_pairs=[("https://x/1.jpg", "m1")])
        task_msg = B.KiraIMMessage(
            extra={"qzone_publish_task": True, "qzone_target_image_count": 1,
                   "qzone_max_image_count": 3}
        )
        event = B.KiraMessageBatchEvent(sid=SID, messages=[task_msg], session=None)
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(event, req, None))
        lines = [l for l in req.user_prompt[0].text.splitlines() if l[:1].isdigit()]
        self.assertEqual(len(lines), 2, "定时发布任务的候选里应剔掉刚发过的那张图")

    def test_hook_keeps_all_for_user_request(self):
        """用户主动发说说时不走去重（保留"指定就发"的语义）。"""
        self.plugin._record_published_images([], md5_pairs=[("https://x/1.jpg", "m1")])
        event = B.KiraMessageBatchEvent(sid=SID, messages=[], session=None)
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(event, req, None))
        lines = [l for l in req.user_prompt[0].text.splitlines() if l[:1].isdigit()]
        self.assertEqual(len(lines), 3, "用户主动发说说时应保留全部候选")


class TestScheduledFillDedupe(B.LoopTestCase):
    def test_fill_skips_published(self):
        self.plugin, self.ctx = B.make_plugin(cfg={'auto_publish_image_min': 2,
                                                   'auto_publish_image_max': 2})
        self.plugin._image_registry[SID] = [
            url_entry("https://x/1.jpg"), url_entry("https://x/2.jpg"), url_entry("https://x/3.jpg"),
        ]
        self.plugin._record_published_images([], md5_pairs=[("https://x/1.jpg", "m1")])
        filled = self.run_(self.plugin._fill_scheduled_publish_sources(SID, [], 2))
        self.assertNotIn("https://x/1.jpg", filled, "补图时不能把刚发过的图再补回来")
        self.assertEqual(len(filled), 2)
