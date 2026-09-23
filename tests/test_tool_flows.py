"""工具全流程回归：八个工具在桩件下必须保持原有语义与返回文案。"""
import asyncio
import time

import _bootstrap as B

main = B.main


def resp(ok=True, data=None, message=None, code=0, raw=None):
    return B._Simple(ok=ok, code=code, message=message,
                     data=data if data is not None else {}, raw=raw if raw is not None else {})


def detail_msg(tid="t1", uin=10001, name="我", text="内容", comments=None, pic=None, liked=0):
    return {
        "tid": tid, "uin": uin, "name": name, "content": text,
        "created_time": int(time.time()), "pic": pic or [],
        "commentlist": comments or [], "isliked": liked, "curlikekey": f"key-{tid}",
    }


class FakeApi:
    def __init__(self):
        self.calls = []
        self.detail = detail_msg()
        self.msglist = [detail_msg()]
        self.comment_ok = True
        self.dup_comment = False
        self.like_ok = True
        self.already_liked = False
        self.like_users = []

    async def get_detail(self, post):
        self.calls.append(("get_detail", post.tid))
        return resp(True, data=dict(self.detail))

    async def get_feeds(self, target_id, pos=0, num=1):
        self.calls.append(("get_feeds", target_id, num))
        return resp(True, data={"msglist": self.msglist[:num]})

    async def get_recent_feeds(self, page=1):
        self.calls.append(("get_recent_feeds",))
        return resp(True, data={"data": {"data": []}})

    async def publish(self, post, allow_image_drop=False):
        self.calls.append(("publish", list(post.images)))
        return resp(True, data={"tid": "new1", "image_md5s": [(str(i), f"m{i}") for i in post.images]})

    async def comment(self, post, content):
        self.calls.append(("comment", post.tid, content))
        return resp(True) if self.comment_ok else resp(False, message="评论失败原因")

    async def like(self, post, abstime=None):
        self.calls.append(("like", post.tid))
        return resp(True) if self.like_ok else resp(False, message="点赞失败原因")

    async def unlike(self, post, abstime=None):
        self.calls.append(("unlike", post.tid))
        return resp(True)

    async def reply(self, post, comment, content, root_comment=None):
        self.calls.append(("reply", post.tid, comment.tid, content))
        return resp(True)

    async def delete(self, tid):
        self.calls.append(("delete", tid))
        return resp(True)

    async def delete_comment(self, uin, tid, comment_id, comment_uin=""):
        self.calls.append(("delete_comment", uin, tid, comment_id, comment_uin))
        return resp(True)

    async def get_like_list(self, post, query_count=20):
        self.calls.append(("get_like_list", post.tid))
        return resp(True, data={"like_uin_info": self.like_users, "like_uins": [],
                                "total_number": len(self.like_users), "is_dolike": self.already_liked})

    async def get_visitor(self):
        self.calls.append(("get_visitor",))
        return resp(True, data={}, raw={"data": {"items": [], "todaycount": 1, "totalcount": 2}})

    async def get_user_info(self, uin):
        return resp(True, data={"nickname": "小助手"})


class ToolFlowCase(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()
        self.plugin.my_uin = 10001
        self.api = FakeApi()
        self.plugin.api = self.api
        self.plugin.session = object()

        async def noop():
            return None

        self.plugin._ensure_api = noop
        self.event = B.KiraMessageBatchEvent(sid="qq:gm:1", messages=[], session=None)

    def call(self, tool_name, **kw):
        fn = getattr(self.plugin, tool_name)
        return self.run_(fn(self.event, **kw))


class TestPublish(ToolFlowCase):
    def test_plain_publish(self):
        out = self.call('tool_publish', text="今天天气不错")
        self.assertIn("说说发布成功！TID: new1", out)

    def test_publish_with_manifest_index(self):
        self.plugin._image_registry["qq:gm:1"] = [
            {"source": "url", "url": "https://x/1.jpg", "sender": "A",
             "time": int(time.time()), "desc": "图一", "msg_id": None}
        ]
        out = self.call('tool_publish', text="配图", image_indices=[1])
        self.assertIn("说说发布成功", out)
        self.assertIn(("publish", ["https://x/1.jpg"]), self.api.calls)

    def test_invalid_index_message_kept(self):
        out = self.call('tool_publish', text="x", image_indices=[9])
        self.assertIn("未能从[近期图片]清单解析出图片", out)
        self.assertNotIn("publish", [c[0] for c in self.api.calls])

    def test_unsafe_url_rejected(self):
        out = self.call('tool_publish', text="x", images=["http://127.0.0.1/a.jpg"])
        self.assertIn("images 参数中的地址均无效", out)


class TestView(ToolFlowCase):
    def test_view_self(self):
        self.api.msglist = [detail_msg(tid="t9", text="你好")]
        out = self.call('tool_view', num=1)
        self.assertIn("【我】(ID:t9)", out)
        self.assertIn("你好", out)

    def test_view_shows_comments_and_likes(self):
        self.api.msglist = [detail_msg(tid="t1", text="正文")]
        self.api.detail = detail_msg(tid="t1", text="正文", comments=[
            {"tid": "c1", "uin": 20002, "name": "小王", "content": "不错", "create_time": 1700000000,
             "commentid": "c1"},
        ])
        self.api.like_users = [{"nick": "小李", "fuin": 30003}]
        out = self.call('tool_view', target_id="20002", num=1)
        self.assertIn("评论区", out)
        self.assertIn("小王", out)
        self.assertIn("已赞1人", out)

    def test_view_blacklist(self):
        self.plugin.qzone_blacklist = ["20002"]
        out = self.call('tool_view', target_id="20002", num=1)
        self.assertIn("查看被拒绝", out)


class TestCommentAndLike(ToolFlowCase):
    def test_comment_with_content(self):
        out = self.call('tool_comment', target_id="20002", tid="t1", content="不错哦")
        self.assertEqual(out, "评论成功")

    def test_comment_idempotent(self):
        self.api.detail = detail_msg(tid="t1", comments=[
            {"tid": "c1", "uin": 10001, "name": "我", "content": "不错哦",
             "create_time": 1700000000, "commentid": "c1"},
        ])
        out = self.call('tool_comment', target_id="20002", tid="t1", content="不错哦")
        self.assertIn("未重复提交", out)

    def test_comment_with_auto_like(self):
        self.plugin.like_when_comment = True
        self.plugin.like_delay_min, self.plugin.like_delay_jitter = 0.0, 0.0
        out = self.call('tool_comment', target_id="20002", tid="t1", content="hi")
        self.assertIn("已同时点赞", out)

    def test_like_and_unlike(self):
        self.assertEqual(self.call('tool_like', target_id="20002", tid="t1"), "点赞成功")
        self.api.detail["isliked"] = 1
        self.assertEqual(self.call('tool_like', target_id="20002", tid="t1"), "这条说说已经赞过了。")

    def test_unlike(self):
        self.assertEqual(self.call('tool_like', target_id="20002", tid="t1", action="unlike"),
                         "取消点赞成功")


class TestDeleteAndReply(ToolFlowCase):
    def test_delete_post(self):
        self.assertEqual(self.call('tool_delete', tid="t1"), "说说 t1 删除成功")

    def test_delete_comment(self):
        out = self.call('tool_delete_comment', target_id="10001", tid="t1", comment_id="c1")
        self.assertEqual(out, "评论删除成功")

    def test_reply_comment(self):
        self.api.detail = detail_msg(tid="t1", comments=[
            {"tid": "c1", "uin": 20002, "name": "小王", "content": "在吗",
             "create_time": 1700000000, "commentid": "c1"},
        ])
        out = self.call('tool_reply_comment', target_id="10001", tid="t1",
                        comment_id="c1", content="在的")
        self.assertEqual(out, "回复成功: 在的")

    def test_visitors(self):
        out = self.call('tool_visitors')
        self.assertIn("最近来访明细", out)


class TestMasterCheck(ToolFlowCase):
    def test_master_check_blocks_when_enabled(self):
        self.plugin.master_check_enabled = True
        self.plugin.master_ids = ["88888"]
        self.event = B.KiraMessageBatchEvent(
            sid="qq:gm:1", messages=[B.KiraIMMessage(sender=B.User(user_id="10000"))], session=None)
        out = self.call('tool_publish', text="x")
        self.assertIn("只有主人", out)


if __name__ == '__main__':
    import unittest
    unittest.main()
