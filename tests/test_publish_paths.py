"""全链路发布路径验证：每一条"能发出去"的路都端到端跑一遍。

覆盖：
  a) 用户主动发纯文字
  b) 用户主动 + images 传本地路径
  c) 用户主动 + want_images 取清单 → 带序号发布
  d) 定时任务：清单序号 + 过期链接 get_msg 续命
  e) 定时任务：清单为空 → 盲补 OneBot 历史图
  f) native 模式：want_images 列出占位串 → 按序号发布（走 elem.to_path）
  g) OneBot 历史图 URL：显式传 images → 续命后再发布
  h) 吸附模式：清单关闭，自动抓最近一张
  i) 清单功能整体关闭时，发布链路照常
"""
import asyncio
import time
import unittest

import _bootstrap as B

main = B.main
SID = "qq:gm:123"
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
NEW_URL = "https://cdn.qq.com/a.jpg?rkey=new"
OLD_URL = "https://cdn.qq.com/a.jpg?rkey=old"


class RoutingAdapter(B.FakeAdapter):
    """按 action 返回不同载荷的假适配器（get_msg / get_group_msg_history）。"""

    def __init__(self, msg_url=None, history_urls=None):
        super().__init__('qq_ada')
        self.msg_url = msg_url
        self.history_urls = history_urls or []
        self.actions = []

    async def send_action(self, action, params, timeout=10.0):
        self.actions.append(action)
        if action == "get_msg":
            if not self.msg_url:
                return {'status': 'ok', 'data': {'message': []}}
            return {'status': 'ok',
                    'data': {'message': [{'type': 'image', 'data': {'url': self.msg_url}}]}}
        if action in ("get_group_msg_history", "get_friend_msg_history"):
            now = int(time.time())
            return {'status': 'ok', 'data': {'messages': [
                {'message_id': 900 + i, 'time': now,
                 'sender': {'nickname': 'A', 'user_id': 1},
                 'message': [{'type': 'image', 'data': {'url': u}}]}
                for i, u in enumerate(self.history_urls)
            ]}}
        return {'status': 'ok', 'data': {}}


class FakeApi:
    def __init__(self):
        self.published = []

    async def publish(self, post, allow_image_drop=False):
        self.published.append(list(post.images))
        return B._Simple(ok=True, code=0, message=None, data={"tid": "t1"}, raw={})


class PublishPathCase(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = RoutingAdapter(msg_url=NEW_URL, history_urls=[NEW_URL])
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': self.adapter})
        self.plugin.my_uin = 10001
        self.api = FakeApi()
        self.plugin.api = self.api
        self.plugin.session = object()

        async def noop():
            return None

        self.plugin._ensure_api = noop

    def ev(self, publish_task=False, target=0, maximum=0):
        msgs = []
        if publish_task:
            msgs.append(B.KiraIMMessage(
                chain=[], group=None,
                extra={"qzone_publish_task": True, "qzone_target_image_count": target,
                       "qzone_max_image_count": maximum or max(target, 3)},
                sender=None))
        return B.KiraMessageBatchEvent(
            sid=SID, messages=msgs,
            session=B.Session(sid=SID, session_type="gm", session_id="123"))

    def url_entry(self, url=OLD_URL, desc="一只橘猫", msg_id=555):
        return {"source": "url", "url": url, "sender": "A",
                "time": int(time.time()), "desc": desc, "msg_id": msg_id}

    def elem_entry(self, caption=None, url="https://cdn.qq.com/rt.jpg"):
        elem = B.Image(url)
        elem.caption = caption
        return {"elem": elem, "sender": "A", "time": int(time.time()),
                "desc": caption, "msg_id": 700}


class TestPublishPaths(PublishPathCase):
    def test_a_plain_text_publish(self):
        out = self.run_(self.plugin.tool_publish(self.ev(), text="早安"))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [[]])

    def test_b_local_path_publish(self):
        out = self.run_(self.plugin.tool_publish(
            self.ev(), text="带图", images=["data/temp/local.jpg"]))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [["data/temp/local.jpg"]])

    def test_c_want_images_then_publish_by_index(self):
        self.plugin._image_registry[SID] = [self.url_entry()]
        out = self.run_(self.plugin.tool_publish(self.ev(), text="今日", want_images=True))
        self.assertIn("说说未发布", out)
        self.assertIn("一只橘猫", out)
        self.assertEqual(self.api.published, [])
        out = self.run_(self.plugin.tool_publish(self.ev(), text="今日", image_indices=[1]))
        self.assertIn("说说发布成功", out)
        # 续命后应使用新链接
        self.assertEqual(self.api.published, [[NEW_URL]])

    def test_d_timer_publish_renews_expired_url(self):
        self.plugin._image_registry[SID] = [self.url_entry()]
        out = self.run_(self.plugin.tool_publish(
            self.ev(publish_task=True, target=1), text="定时", image_indices=[1]))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [[NEW_URL]])
        self.assertIn("get_msg", self.adapter.actions)

    def test_e_timer_blind_fills_from_onebot_history(self):
        """清单为空时，定时发布仍能从 OneBot 历史盲补一张（不需要描述）。"""
        self.plugin._image_registry[SID] = []
        out = self.run_(self.plugin.tool_publish(
            self.ev(publish_task=True, target=1), text="定时"))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [[NEW_URL]])
        self.assertIn("get_group_msg_history", self.adapter.actions)

    def test_f_native_mode_publish_by_index(self):
        entry = self.elem_entry(caption="attached image")
        self.plugin._image_registry[SID] = [entry]
        out = self.run_(self.plugin.tool_publish(self.ev(), text="今日", want_images=True))
        self.assertIn("说说未发布", out)
        self.assertIn("attached image", out, 'native 占位串按原样展示')
        out = self.run_(self.plugin.tool_publish(self.ev(), text="今日", image_indices=[1]))
        self.assertIn("说说发布成功", out)
        self.assertEqual(len(self.api.published), 1)
        self.assertEqual(len(self.api.published[0]), 1, "应解析出本地路径再发布")

    def test_g_explicit_history_url_renewed(self):
        """OneBot 历史图的 URL（已登记、带 message_id）：显式传 images 也要能救活。"""
        self.plugin._image_registry[SID] = [self.url_entry(desc=None)]
        out = self.run_(self.plugin.tool_publish(self.ev(), text="用历史图", images=[OLD_URL]))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [[NEW_URL]])
        self.assertIn("get_msg", self.adapter.actions)

    def test_g2_explicit_unknown_url_passes_through(self):
        out = self.run_(self.plugin.tool_publish(
            self.ev(), text="外部图", images=["https://cdn.qq.com/other.jpg?rkey=x"]))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [["https://cdn.qq.com/other.jpg?rkey=x"]])


class TestLegacyAutoPublish(B.LoopTestCase):
    """「后台直接生成模式」：候选只用已知描述（免费路径），拿不到就降级纯文字。"""

    def setUp(self):
        super().setUp()
        self.adapter = RoutingAdapter(history_urls=[NEW_URL])
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': self.adapter})
        self.plugin.my_uin = 10001
        self.plugin.auto_publish_group_id = "123"
        self.plugin.auto_publish_image_min = 0
        self.plugin.auto_publish_image_max = 3
        self.plugin.auto_publish_image_prob = 1.0
        self.plugin.auto_publish_image_dedupe_interval = None
        self.published = []

        async def fake_llm(prompt, system_prompt=None, use_backend_model=False):
            return "今天的晚霞很好看\nIMG:1"

        async def fake_publish(text, images, allow_image_drop=False):
            self.published.append(list(images))
            return "说说发布成功！TID: t1"

        async def noop():
            return None

        self.plugin._call_llm = fake_llm
        self.plugin._publish = fake_publish
        self.plugin._ensure_api = noop

    def test_candidates_use_cached_description_only(self):
        # 同 URL 的图在清单里有框架描述 → 可以当候选
        self.plugin._image_registry["qq:gm:123"] = [
            {"source": "url", "url": NEW_URL, "sender": "A",
             "time": int(time.time()), "desc": "一只橘猫", "msg_id": None}
        ]
        self.run_(self.plugin._legacy_auto_publish(1))
        self.assertEqual(self.published, [[NEW_URL]])

    def test_candidates_without_description_fall_back_to_text(self):
        # 清单里没有任何"已知描述"的图 → 不配图，纯文字发布
        self.plugin._image_registry["qq:gm:123"] = []
        self.run_(self.plugin._legacy_auto_publish(1))
        self.assertEqual(self.published, [[]])
        self.assertEqual(self.adapter.actions.count("get_group_msg_history") >= 1, True)


class TestAttachModeAndSwitches(B.LoopTestCase):
    """吸附模式 / 清单关闭：发布链路照常。"""

    def setUp(self):
        super().setUp()
        self.adapter = RoutingAdapter(history_urls=[NEW_URL])
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': self.adapter})
        self.plugin.my_uin = 10001
        self.api = FakeApi()
        self.plugin.api = self.api
        self.plugin.session = object()

        async def noop():
            return None

        self.plugin._ensure_api = noop

    def ev(self):
        return B.KiraMessageBatchEvent(
            sid=SID, messages=[],
            session=B.Session(sid=SID, session_type="gm", session_id="123"))

    def test_h_attach_mode_grabs_recent_image(self):
        self.plugin.auto_attach_recent_image = True
        out = self.run_(self.plugin.tool_publish(self.ev(), text="吸附"))
        self.assertIn("说说发布成功", out)
        self.assertEqual(self.api.published, [[NEW_URL]])

    def test_h2_attach_mode_gives_no_manifest(self):
        self.plugin.auto_attach_recent_image = True
        self.plugin._image_registry[SID] = [
            {"source": "url", "url": OLD_URL, "sender": "A", "time": int(time.time()),
             "desc": "猫", "msg_id": None}
        ]
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(self.ev(), req, None))
        self.assertEqual(req.user_prompt, [], '吸附模式不注入清单')

    def test_i_manifest_disabled_publish_still_works(self):
        self.plugin.image_manifest_enabled = False
        self.run_(self.plugin._collect_images(B.KiraMessageEvent(
            message=B.KiraIMMessage(chain=[B.Image("https://x/a.jpg")], sender=None,
                                    timestamp=int(time.time())),
            session=B.Session(sid=SID))))
        self.assertEqual(self.plugin._image_registry, {}, '清单关闭时不登记')
        out = self.run_(self.plugin.tool_publish(
            self.ev(), text="带图", images=["https://cdn.qq.com/x.jpg?rkey=1"]))
        self.assertIn("说说发布成功", out)


class FakeProcessor:
    """接住合成事件的 message_processor。"""

    def __init__(self):
        self.events = []

    async def handle_im_message(self, event):
        self.events.append(event)
        return True


class TestScheduledJobWiring(B.LoopTestCase):
    """定时任务（群聊指令模式）：job → 合成事件（带标记）→ 交给消息处理器。

    这是 README 里推荐的模式，之前没有端到端覆盖。
    """

    def setUp(self):
        super().setUp()
        self.adapter = RoutingAdapter(history_urls=[NEW_URL])
        self.plugin, self.ctx = B.make_plugin(adapters={'qq_ada': self.adapter})
        self.plugin.my_uin = 10001
        self.plugin.task_group_ids = ["123"]
        self.plugin.auto_publish_image_min = 2
        self.plugin.auto_publish_image_max = 2
        self.ctx.message_processor = FakeProcessor()

        async def noop():
            return None

        self.plugin._ensure_api = noop

    def _text(self, event):
        return "".join(getattr(e, "text", "") or "" for e in event.message.chain)

    def test_publish_job_sends_marked_instruction(self):
        self.run_(self.plugin._auto_publish_job())
        self.assertEqual(len(self.ctx.message_processor.events), 1, "应合成一条定时任务事件")
        event = self.ctx.message_processor.events[0]
        extra = event.message.extra
        self.assertTrue(extra.get("qzone_publish_task"),
                        "必须带发布任务标记（tool_publish 靠它识别定时任务）")
        self.assertEqual(extra.get("qzone_target_image_count"), 2)
        self.assertEqual(extra.get("qzone_max_image_count"), 2)
        text = self._text(event)
        self.assertIn("定时任务", text)
        self.assertIn("image_indices", text, "指令里要告诉她怎么配图")
        self.assertEqual(event.session.session_type, "gm")
        self.assertEqual(event.session.session_id, "123")

    def test_comment_job_sends_instruction(self):
        self.run_(self.plugin._auto_comment_job())
        self.assertEqual(len(self.ctx.message_processor.events), 1)
        self.assertIn("评论任务", self._text(self.ctx.message_processor.events[0]))

    def test_reply_job_mentions_existing_tool(self):
        """回复任务指令里点名的工具必须真实存在（本轮还原了 qzone_reply_comment）。"""
        self.run_(self.plugin._auto_reply_job())
        self.assertEqual(len(self.ctx.message_processor.events), 1)
        self.assertIn("qzone_reply_comment", self._text(self.ctx.message_processor.events[0]))
        self.assertTrue(hasattr(self.plugin, "tool_reply_comment"), "指令点名的工具必须存在")

    def test_blackout_skips_all_jobs(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        span = (f"{(now - timedelta(minutes=5)).strftime('%H:%M')}-"
                f"{(now + timedelta(minutes=5)).strftime('%H:%M')}")
        self.plugin.blackout_schedules = [span]
        for job in (self.plugin._auto_publish_job, self.plugin._auto_comment_job):
            self.run_(job())
        self.plugin.auto_reply_enabled = True
        self.run_(self.plugin._auto_reply_job())
        self.assertEqual(self.ctx.message_processor.events, [],
                         "黑名单时段内不应发起任何定时任务")
