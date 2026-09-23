"""解析层容错：一条脏数据不能吞掉整页说说（旧实现会整页返回空）。"""
import unittest

import _bootstrap as B

parser = B.qzone_parser


def post(**kw):
    base = {
        "tid": "abc123",
        "uin": 10001,
        "name": "某人",
        "content": "今天天气不错",
        "created_time": 1700000000,
        "pic": [{"url2": "https://example.com/a.jpg"}],
        "commentlist": [],
    }
    base.update(kw)
    return base


class TestParseFeedsTolerance(unittest.TestCase):
    def test_normal_post(self):
        posts = parser.QzoneParser.parse_feeds([post()])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].tid, "abc123")
        self.assertEqual(posts[0].uin, 10001)
        self.assertEqual(posts[0].images, ["https://example.com/a.jpg"])

    def test_pic_is_null(self):
        """pic 为 null：旧实现 msg.get('pic', []) 返回 None → TypeError → 整页丢弃。"""
        posts = parser.QzoneParser.parse_feeds([post(pic=None)])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].images, [])

    def test_created_time_none(self):
        posts = parser.QzoneParser.parse_feeds([post(created_time=None)])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].create_time, 0)

    def test_created_time_string(self):
        posts = parser.QzoneParser.parse_feeds([post(created_time="1700000001")])
        self.assertEqual(posts[0].create_time, 1700000001)

    def test_uin_not_numeric(self):
        posts = parser.QzoneParser.parse_feeds([post(uin="abc")])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].uin, 0)

    def test_tid_none(self):
        posts = parser.QzoneParser.parse_feeds([post(tid=None)])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].tid, "0")

    def test_video_null_and_pic_null(self):
        posts = parser.QzoneParser.parse_feeds([post(pic=None, video=None)])
        self.assertEqual(len(posts), 1)

    def test_comment_uin_not_numeric(self):
        page = [post(commentlist=[{"uin": "x", "name": "n", "content": "c", "tid": "1"}])]
        posts = parser.QzoneParser.parse_feeds(page)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0].comments), 1)

    def test_dirty_post_does_not_kill_page(self):
        """关键回归：好-坏-好 三页，坏的那条被跳过，其余两条必须存活。"""
        page = [post(tid="good1"), post(tid="bad", pic="not-a-list-marker"), post(tid="good2")]
        posts = parser.QzoneParser.parse_feeds(page)
        tids = [p.tid for p in posts]
        self.assertIn("good1", tids)
        self.assertIn("good2", tids)
        self.assertGreaterEqual(len(posts), 2)

    def test_msglist_not_list(self):
        self.assertEqual(parser.QzoneParser.parse_feeds(None), [])
        self.assertEqual(parser.QzoneParser.parse_feeds({"a": 1}), [])

    def test_like_info_null(self):
        posts = parser.QzoneParser.parse_feeds([post(likeinfo=None)])
        self.assertEqual(posts[0].like_count, 0)
        self.assertEqual(posts[0].like_users, [])


class TestParseUploadResult(unittest.TestCase):
    GOOD = {
        "data": {
            "url": "https://up.qzone.qq.com/x?a=1&bo=abc123",
            "albumid": "alb", "lloc": "l", "sloc": "s", "type": "jpg",
            "height": "100", "width": "200",
        }
    }

    def test_valid(self):
        picbo, richval = parser.QzoneParser.parse_upload_result(self.GOOD)
        self.assertEqual(picbo, "abc123")
        self.assertTrue(richval.startswith(","))

    def test_missing_data(self):
        with self.assertRaises(ValueError):
            parser.QzoneParser.parse_upload_result({"ret": 0})

    def test_url_without_bo(self):
        bad = {"data": dict(self.GOOD["data"], url="https://x/y")}
        with self.assertRaises(ValueError):
            parser.QzoneParser.parse_upload_result(bad)

    def test_missing_field(self):
        bad = {"data": {k: v for k, v in self.GOOD["data"].items() if k != "albumid"}}
        with self.assertRaises(ValueError):
            parser.QzoneParser.parse_upload_result(bad)


class TestParseVisitors(unittest.TestCase):
    def test_dirty_counts(self):
        text = parser.QzoneParser.parse_visitors(
            {"data": {"items": [{"time": 1700000000, "name": "A", "src": 0}],
                      "todaycount": None, "totalcount": "12"}}
        )
        self.assertIn("今日访客共 0 人", text)
        self.assertIn("最近30天访客共 12 人", text)

    def test_empty(self):
        self.assertIn("暂无访客记录", parser.QzoneParser.parse_visitors({}))


if __name__ == '__main__':
    unittest.main()
