"""纯逻辑函数测试：调度解析、配图选择、评论 ID 匹配、黑白名单、时间格式化等。

这些是"逻辑密集但不涉及 IO"的地方 —— 重构最容易悄悄改坏，必须钉住。
"""
import time
import unittest

import _bootstrap as B

main = B.main
from qzone_plugin.qzone.model import Comment


def cmt(tid=1, uin=20002, nick="小王", content="你好", comment_id="", parent_tid=None):
    return Comment(uin=uin, nickname=nick, content=content, create_time=1700000000,
                   tid=tid, comment_id=comment_id or str(tid), parent_tid=parent_tid)


class TestIntervalParsing(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = B.make_plugin()

    def test_interval_units(self):
        p = self.plugin._parse_interval_seconds
        self.assertEqual(p("3d"), 3 * 86400)
        self.assertEqual(p("6h"), 6 * 3600)
        self.assertEqual(p("30m"), 1800)
        self.assertEqual(p("45s"), 45)
        # 裸数字按 default_unit 解释
        self.assertEqual(p("30", default_unit="m"), 1800)
        self.assertEqual(p("7200", default_unit="s"), 7200)
        self.assertEqual(p("2", default_unit="h"), 7200)

    def test_cookie_interval_bare_number_is_seconds(self):
        """文档写"支持 7200（秒）"，所以 cookie 周期刷新的裸数字必须按秒。"""
        plugin, _ = B.make_plugin(cfg={'cookie_refresh_interval': '7200'})
        self.assertEqual(plugin.cookie_refresh_interval, 7200)

    def test_interval_disabled_values(self):
        p = self.plugin._parse_interval_seconds
        for v in (None, "", "0", "0s", "0m", "0h", "0d"):
            self.assertIsNone(p(v), v)

    def test_interval_garbage_falls_back(self):
        p = self.plugin._parse_interval_seconds
        self.assertIsNone(p("abc"))
        self.assertIsNone(p("一小时"))

    def test_delay_range(self):
        d = self.plugin._parse_delay_range
        lo, jit = d("0.5-1.5s")
        self.assertAlmostEqual(lo, 0.5)
        self.assertAlmostEqual(jit, 1.0)
        lo, jit = d("2-4")
        self.assertAlmostEqual(lo, 2.0)
        self.assertAlmostEqual(jit, 2.0)
        # 非法值回退默认
        lo, jit = d("abc", default="0.5-1.5s")
        self.assertAlmostEqual(lo, 0.5)
        self.assertAlmostEqual(jit, 1.0)

    def test_schedule_cron(self):
        s = self.plugin._parse_schedule
        got = s("0 8 * * *")
        self.assertEqual(got["mode"], "cron")
        self.assertEqual(got["expr"], "0 8 * * *")

    def test_schedule_interval_and_jitter(self):
        s = self.plugin._parse_schedule
        got = s("2h")
        self.assertEqual(got, {"mode": "interval", "interval_seconds": 7200, "jitter_seconds": 0})
        got = s("2h/30m")
        self.assertEqual(got["interval_seconds"], 7200)
        self.assertEqual(got["jitter_seconds"], 1800)

    def test_schedule_invalid(self):
        self.assertIsNone(self.plugin._parse_schedule(""))
        self.assertIsNone(self.plugin._parse_schedule("   "))
        self.assertIsNone(self.plugin._parse_schedule("abc"))


class TestImgChoiceParsing(unittest.TestCase):
    def test_single_and_multi(self):
        got = main.QzonePlugin._split_img_choices("今天天气不错\nIMG:1")
        self.assertEqual(got, ("今天天气不错", [1]))
        got = main.QzonePlugin._split_img_choices("文字\nIMG:1,3")
        self.assertEqual(got, ("文字", [1, 3]))
        got = main.QzonePlugin._split_img_choices("文字\nIMG：2")   # 全角冒号
        self.assertEqual(got, ("文字", [2]))

    def test_no_marker(self):
        self.assertEqual(main.QzonePlugin._split_img_choices("纯文字"), ("纯文字", []))
        self.assertEqual(main.QzonePlugin._split_img_choices(""), ("", []))

    def test_garbage_ignored(self):
        """非法 IMG 行不会被吞掉，而是原样留在正文里（旧行为，保持一致）。"""
        got = main.QzonePlugin._split_img_choices("文字\nIMG:abc")
        self.assertEqual(got, ("文字\nIMG:abc", []))


class TestImagePolicy(unittest.TestCase):
    def setUp(self):
        self.pol = B.qzone_image_policy

    def test_draw_target_in_range(self):
        for _ in range(20):
            v = self.pol.draw_target(1, 3)
            self.assertTrue(1 <= v <= 3)

    def test_instruction_text(self):
        self.assertIn("0", self.pol.build_instruction(0, 3))
        self.assertIn("2", self.pol.build_instruction(2, 3))

    def test_dedupe_and_label(self):
        self.assertEqual(self.pol.dedupe_sources(["a", "b", "a", ""]), ["a", "b"])
        self.assertEqual(self.pol.candidate_label(""), "图片内容暂未识别（仍可作为配图选择）")
        self.assertEqual(self.pol.candidate_label("猫"), "猫")

    def test_resolve_sources_pads_to_target(self):
        sources = ["s1", "s2", "s3"]
        got = self.pol.resolve_described_sources(sources, [1], target=2, maximum=3)
        self.assertEqual(got, ["s1", "s2"])

    def test_resolve_sources_respects_choice_when_target_zero(self):
        sources = ["s1", "s2", "s3"]
        got = self.pol.resolve_described_sources(sources, [1, 3], target=0, maximum=3)
        self.assertEqual(got, ["s1", "s3"])

    def test_resolve_sources_ignores_out_of_range(self):
        got = self.pol.resolve_described_sources(["s1"], [9], target=0, maximum=3)
        self.assertEqual(got, [])


class TestBlackWhiteList(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = B.make_plugin()
        self.plugin.my_uin = 10001

    def test_blacklist_first(self):
        self.plugin.qzone_blacklist = ["123"]
        self.plugin.qzone_whitelist = ["123"]
        self.assertIsNotNone(self.plugin._target_block_reason("123"))

    def test_whitelist_only_listed_and_self(self):
        self.plugin.qzone_whitelist = ["777"]
        self.assertIsNone(self.plugin._target_block_reason("777"))
        self.assertIsNone(self.plugin._target_block_reason("10001"))   # 自己
        self.assertIsNotNone(self.plugin._target_block_reason("888"))

    def test_empty_lists_allow_all(self):
        self.assertIsNone(self.plugin._target_block_reason("999"))
        self.assertIsNone(self.plugin._target_block_reason(""))


class TestCommentHelpers(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = B.make_plugin()
        self.plugin.my_uin = 10001

    def test_parse_comment_content(self):
        target, text = self.plugin._parse_comment_content(
            "@{uin:20002,nick:小王,who:1,auto:1}你好呀")
        self.assertEqual(target, "小王(UIN:20002)")
        self.assertEqual(text, "你好呀")
        target, text = self.plugin._parse_comment_content("普通评论")
        self.assertEqual((target, text), ("", "普通评论"))

    def test_normalize_and_count_own(self):
        from qzone_plugin.qzone.model import Post
        post = Post(tid="t1", uin=10001, comments=[
            cmt(tid=1, uin=10001, content="好 看"),
            cmt(tid=2, uin=20002, content="好 看"),
        ])
        self.assertEqual(self.plugin._count_own_comment(post, "好看"), 1)
        self.assertEqual(self.plugin._count_own_comment(post, "没有的"), 0)

    def test_match_comment_by_comment_id_or_tid_or_uin(self):
        comments = [cmt(tid=1, uin=20002, comment_id="1001"),
                    cmt(tid=2, uin=30003, comment_id="1002")]
        self.assertIsNotNone(self.plugin._match_comment(comments, "1001"))
        self.assertIsNotNone(self.plugin._match_comment(comments, "2"))
        self.assertIsNotNone(self.plugin._match_comment(comments, "1", "20002"))
        self.assertIsNone(self.plugin._match_comment(comments, "1", "99999"))
        self.assertIsNone(self.plugin._match_comment(comments, "999"))

    def test_match_comment_ambiguous_returns_none(self):
        # 同一说说里两层楼中回复可能共用一个短楼层号
        comments = [cmt(tid=1, uin=20002, comment_id="same"),
                    cmt(tid=2, uin=30003, comment_id="same")]
        self.assertIsNone(self.plugin._match_comment(comments, "same"))

    def test_format_comment_line_prefers_real_id(self):
        line = self.plugin._format_comment_line(cmt(tid=7, comment_id="9001"), "主评论", "  ", "t")
        self.assertIn("ID:9001", line)
        self.assertIn("UIN:20002", line)

    def test_find_root_comment(self):
        root = cmt(tid=10, comment_id="r")
        sub = cmt(tid=11, comment_id="s", parent_tid=10)
        other = cmt(tid=12, comment_id="o")
        self.assertIs(self.plugin._find_root_comment([root, sub, other], sub), root)
        self.assertIs(self.plugin._find_root_comment([root, sub, other], root), root)

    def test_find_root_comment_ambiguous_raises(self):
        root1 = cmt(tid=10, comment_id="r1")
        root2 = cmt(tid=10, comment_id="r2")
        sub = cmt(tid=11, comment_id="s", parent_tid=10)
        with self.assertRaises(RuntimeError):
            self.plugin._find_root_comment([root1, root2, sub], sub)


class TestManifestHelpers(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = B.make_plugin()

    def test_format_manifest_time_tolerates_garbage(self):
        f = main.QzonePlugin._format_manifest_time
        self.assertEqual(f("abc"), "时间未知")
        self.assertEqual(f(None), "时间未知")
        self.assertEqual(f(0), "时间未知")
        self.assertEqual(len(f(int(time.time()))), 11)   # MM-DD HH:MM

    def test_identity_key_normalizes_url(self):
        key = main.QzonePlugin._identity_key
        self.assertEqual(key(" https://x/a.jpg "), "https://x/a.jpg")
        self.assertEqual(key("data/temp/a.jpg"), "data/temp/a.jpg")
        self.assertEqual(key(""), "")

    def test_prune_image_registry_caps_sessions(self):
        cap = main.IMAGE_REGISTRY_MAX_SESSIONS
        for i in range(cap + 10):
            self.plugin._image_registry[f"qq:gm:{i}"] = []
        self.plugin._prune_image_registry()
        self.assertLessEqual(len(self.plugin._image_registry), cap)

    def test_cache_pruning_bounds(self):
        for i in range(main.URL_MD5_MAX + 50):
            self.plugin._entry_desc[f"url:{i}"] = "d"
            self.plugin._url_md5[f"u{i}"] = "m"
        self.plugin._prune_entry_caches()
        self.assertLessEqual(len(self.plugin._entry_desc), main.URL_MD5_MAX)
        self.assertLessEqual(len(self.plugin._url_md5), main.URL_MD5_MAX)


class TestClampAndConfig(unittest.TestCase):
    def test_clamp_float(self):
        self.plugin, _ = B.make_plugin()
        self.assertEqual(self.plugin._clamp_float(2.0, 0.0, 1.0), 1.0)
        self.assertEqual(self.plugin._clamp_float(-1, 0.0, 1.0), 0.0)
        self.assertEqual(self.plugin._clamp_float("abc", 0.0, 1.0), 0.0)

    def test_bool_config_from_string(self):
        """WebUI 传字符串 'false' 时不能被 bool() 当成 True。"""
        plugin, _ = B.make_plugin(cfg={'cookie_self_heal': False, 'image_manifest_enabled': False})
        self.assertFalse(plugin.cookie_self_heal)
        self.assertFalse(plugin.image_manifest_enabled)
