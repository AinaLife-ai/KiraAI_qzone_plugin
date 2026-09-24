"""钩子守卫（按"只走免费路径"的新策略重写）。

新策略的核心约定：
- 清单**只列框架已经描述过的图片**（读 elem.caption / 内存里已知的描述）；
- 拿不到描述 → 该图不进清单，**不下载、不识图**；
- 因此钩子本身**没有任何 await / 网络 / 磁盘操作**，也不可能阻塞请求。
"""
import ast
import asyncio
import pathlib
import time

import _bootstrap as B

main = B.main
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
SID = "qq:gm:123"


def described_entry(url, desc="一只橘猫", sender="某人", when=None):
    return {"source": "url", "url": url, "sender": sender,
            "time": when if when is not None else int(time.time()),
            "desc": desc, "msg_id": None}


def undocumented_entry(url):
    return {"source": "url", "url": url, "sender": "某人",
            "time": int(time.time()), "desc": None, "msg_id": None}


def make_event(sid=SID, text=None):
    msgs = [B.KiraIMMessage(chain=[B.Text(text)] if text else [], extra={}, sender=None)] if text else []
    return B.KiraMessageBatchEvent(sid=sid, messages=msgs, session=None)


class TestHookIsPureRead(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        # 既有清单用例按"每轮注入"语义写；on_demand 由专门用例覆盖
        self.plugin, self.ctx = B.make_plugin()
        self.plugin._image_registry[SID] = [described_entry(f"https://x/{i}.jpg") for i in range(5)]

    def test_hook_has_no_await_at_all(self):
        """最强判据：钩子函数体里连一个 await 都没有 —— 结构上不可能阻塞。"""
        src = pathlib.Path(B.ROOT / 'main.py').read_text(encoding='utf-8')
        target = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == '_inject_image_manifest':
                target = node
        self.assertIsNotNone(target)
        awaits = [n for n in ast.walk(target) if isinstance(n, ast.Await)]
        self.assertEqual(awaits, [], f'钩子里出现了 {len(awaits)} 处 await')
        async_funcs = [n for n in ast.walk(target)
                       if isinstance(n, ast.AsyncFunctionDef) and n is not target]
        self.assertEqual(async_funcs, [], '钩子里不应有内嵌协程')

    def test_hook_calls_nothing_expensive(self):
        """静态守卫：不得出现下载/识图/取本地文件/睡眠类调用。"""
        src = pathlib.Path(B.ROOT / 'main.py').read_text(encoding='utf-8')
        target = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == '_inject_image_manifest':
                target = node
        forbidden = {'download_file', 'fetch_bytes', 'desc_img', 'hash_image', 'to_path',
                     'sleep', 'wait_for', 'gather', 'to_thread', 'create_task'}
        found = set()
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr in forbidden:
                    found.add(f.attr)
                elif isinstance(f, ast.Name) and f.id in forbidden:
                    found.add(f.id)
        self.assertEqual(found, set(), f'钩子里出现了这些调用: {found}')

    def test_hook_does_no_io_even_with_slow_stubs(self):
        """慢下载 + 慢数据库桩：钩子期间两者都不该被碰到，且耗时极短。"""
        calls = []

        async def slow_fetch(url, **kw):
            calls.append(("fetch", url))
            await asyncio.sleep(5)
            return B.fetch_result(True, data=JPEG)

        async def slow_db(md5):
            calls.append(("db", md5))
            await asyncio.sleep(5)
            return None

        B.patch_fetch_bytes(slow_fetch)
        self.ctx.db.get_image_desc_cache = slow_db
        req = B.LLMRequest()
        start = time.monotonic()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.05, f'钩子耗时 {elapsed*1000:.1f}ms')
        self.assertEqual(calls, [], f'钩子不该发起任何 IO: {calls}')
        self.assertEqual(len(req.user_prompt), 1)

    def test_hook_creates_no_background_task(self):
        """钩子只读：不产生任何后台任务（更不会去识图）。"""
        before = len(self.plugin._bg_tasks)
        self.run_(self.plugin._inject_image_manifest(make_event(), B.LLMRequest(), None))
        self.assertEqual(len(self.plugin._bg_tasks), before, '钩子不应产生后台任务')

    def test_hook_never_touches_image_element(self):
        elem = B.Image("https://x/rt.jpg")
        elem.caption = "框架给的描述"
        self.plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()), "desc": None, "msg_id": 1}
        ]
        self.run_(self.plugin._inject_image_manifest(make_event(), B.LLMRequest(), None))
        self.assertEqual(elem.hash_image_calls, 0, '不许调用 hash_image（框架里它会真下载）')
        self.assertEqual(elem.to_path_calls, 0, '不许取本地文件')


class TestOnlyFrameworkDescribedAreListed(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()

    def test_undocumented_images_are_dropped_not_listed(self):
        """没有框架描述的图片：不进清单（既不列表也不识图）。"""
        self.plugin._image_registry[SID] = [
            described_entry("https://x/1.jpg", "有描述1"),
            undocumented_entry("https://x/2.jpg"),
            described_entry("https://x/3.jpg", "有描述3"),
        ]
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        text = req.user_prompt[0].text
        lines = [l for l in text.splitlines() if l[:1].isdigit()]
        self.assertEqual(len(lines), 2, f'只应列出有描述的两张: {lines}')
        self.assertIn("有描述1", text)
        self.assertIn("有描述3", text)
        self.assertNotIn("暂未识别", text, '不再输出占位文案')

    def test_all_undocumented_means_no_injection(self):
        self.plugin._image_registry[SID] = [undocumented_entry("https://x/1.jpg")]
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        self.assertEqual(req.user_prompt, [], '一张可用候选都没有时，一句都不注入')

    def test_framework_caption_is_used_as_is(self):
        elem = B.Image("https://x/rt.jpg")
        elem.caption = "框架已经识别好的描述"
        self.plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()), "desc": None, "msg_id": 1}
        ]
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        self.assertIn("框架已经识别好的描述", req.user_prompt[0].text)

    def test_numbering_is_contiguous_after_filtering(self):
        """过滤后序号必须连续，且与发布解析用同一份清单。"""
        self.plugin._image_registry[SID] = [
            described_entry("https://x/1.jpg", "图一"),
            undocumented_entry("https://x/2.jpg"),
            described_entry("https://x/3.jpg", "图三"),
        ]
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        lines = [l for l in req.user_prompt[0].text.splitlines() if l[:1].isdigit()]
        self.assertTrue(lines[0].startswith("1."))
        self.assertTrue(lines[1].startswith("2."))
        # 序号 1 应解析到第一张有描述的图
        resolved = self.run_(self.plugin._resolve_manifest_images(SID, [1]))
        self.assertEqual(resolved, ["https://x/1.jpg"])
