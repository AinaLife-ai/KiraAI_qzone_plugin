"""需求 1 验收：llm_request 钩子不得阻塞（下载/VLM 一律只能在后台）。"""
import ast
import asyncio
import pathlib
import time

import _bootstrap as B

main = B.main

JPEG = b"\xff\xd8\xff" + b"\x00" * 64
SID = "qq:gm:123"


def make_entries(sid=SID, count=5, desc=None):
    now = int(time.time())
    return [
        {"source": "url", "url": f"https://x/{i}.jpg", "sender": "某人",
         "time": now, "desc": desc, "msg_id": 100 + i}
        for i in range(count)
    ]


def make_event(sid=SID):
    return B.KiraMessageBatchEvent(
        sid=sid, messages=[],
        session=B.Session(sid=sid, session_type="gm", session_id="123"),
    )


class TestHookBudget(B.LoopTestCase):
    def setUp(self):
        super().setUp()
        self.plugin, self.ctx = B.make_plugin()
        self.plugin._image_registry[SID] = make_entries()

    def test_hook_does_not_wait_for_download(self):
        """钩子内出现慢下载桩时，钩子必须"秒回"，且期间不发起下载。"""
        calls = []

        async def slow_fetch(url, **kw):
            calls.append((url, time.monotonic()))
            await asyncio.sleep(5)
            return B.fetch_result(True, data=JPEG)

        B.patch_fetch_bytes(slow_fetch)
        req = B.LLMRequest()
        marks = {}

        async def call_hook():
            await self.plugin._inject_image_manifest(make_event(), req, None)
            marks['end'] = time.monotonic()

        start = time.monotonic()
        self.run_(call_hook())
        elapsed = marks['end'] - start
        self.assertLess(elapsed, 0.3, f"钩子耗时 {elapsed:.3f}s，仍然阻塞")
        # 判据：没有任何一次下载在钩子返回之前启动（之后启动是后台任务，正常）
        early = [u for u, ts in calls if ts < marks['end']]
        self.assertEqual(early, [], f"这些下载在钩子内启动了: {early}")
        self.assertEqual(len(req.user_prompt), 1)
        self.assertIn("暂未识别", req.user_prompt[0].text)
        self.assertEqual(len(self.plugin._desc_tasks), 5, "应为每张未识别图片触发后台任务")

    def test_hook_never_touches_image_element(self):
        """实时图片条目：钩子绝不能摸 elem.hash_image()（框架里它会真下载）。"""
        elem = B.Image("https://x/rt.jpg")
        self.plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()), "desc": None, "msg_id": 1}
        ]
        marks = {}

        async def call_hook():
            await self.plugin._inject_image_manifest(make_event(), B.LLMRequest(), None)
            marks['to_path'] = elem.to_path_calls
            marks['hash'] = elem.hash_image_calls

        self.run_(call_hook())
        self.assertEqual(marks['hash'], 0, "钩子不应调用 hash_image()")
        self.assertEqual(marks['to_path'], 0, "钩子不应调用 to_path()")
        # 后台任务随后可以（也应该）去取本地文件
        self.run_(asyncio.sleep(0.2))
        self.assertEqual(elem.hash_image_calls, 0, "整条链路都不该调 hash_image()")
        self.assertGreaterEqual(elem.to_path_calls, 1, "后台任务应去取本地文件")

    def test_caption_used_without_vlm(self):
        elem = B.Image("https://x/rt.jpg", caption="框架已识别的描述")
        self.plugin._image_registry[SID] = [
            {"elem": elem, "sender": "A", "time": int(time.time()), "desc": None, "msg_id": 1}
        ]
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        self.assertIn("框架已识别的描述", req.user_prompt[0].text)

    def test_description_ready_on_next_round(self):
        """效果不掉：后台产出后，下一轮注入必须带上真实描述。"""
        async def fast_fetch(url, **kw):
            return B.fetch_result(True, data=JPEG)

        async def fake_desc(**kw):
            return "第X张图的内容"

        B.patch_fetch_bytes(fast_fetch)
        B.patch_desc_img(fake_desc)
        self.run_(self.plugin._inject_image_manifest(make_event(), B.LLMRequest(), None))
        self.run_(asyncio.sleep(0.5))
        req = B.LLMRequest()
        self.run_(self.plugin._inject_image_manifest(make_event(), req, None))
        self.assertIn("第X张图的内容", req.user_prompt[0].text)

    def test_vlm_failure_marks_negative_cache(self):
        """识图失败要落负缓存，且只在后台发生。"""
        async def fast_fetch(url, **kw):
            return B.fetch_result(True, data=JPEG)

        async def boom(**kw):
            raise RuntimeError("VLM 挂了")

        B.patch_fetch_bytes(fast_fetch)
        B.patch_desc_img(boom)
        self.run_(self.plugin._inject_image_manifest(make_event(), B.LLMRequest(), None))
        self.run_(asyncio.sleep(0.5))
        self.assertEqual(len(self.plugin._desc_failed), 5)

    def test_hook_budget_is_bounded_by_config(self):
        """即使把所有描述的 DB 查询变成慢查询，钩子也必须在预算内返回。"""
        async def slow_db(md5):
            await asyncio.sleep(5)
            return ""

        self.ctx.db.get_image_desc_cache = slow_db
        for entry in self.plugin._image_registry[SID]:
            entry["url"] = entry["url"]  # 保持 URL 条目
            self.plugin._entry_md5[self.plugin._entry_key(entry)] = "deadbeef"
        self.plugin.manifest_hook_budget_ms = 100
        start = time.monotonic()
        self.run_(self.plugin._inject_image_manifest(make_event(), B.LLMRequest(), None))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.6, f"钩子耗时 {elapsed:.3f}s，超出了预算保护")

    def test_static_guard_no_blocking_calls_in_hook(self):
        """静态守卫：钩子函数体内不得出现下载/识图/睡眠类调用。"""
        src = pathlib.Path(B.ROOT / 'main.py').read_text(encoding='utf-8')
        target = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '_inject_image_manifest':
                target = node
        self.assertIsNotNone(target)
        forbidden = {'download_file', 'fetch_bytes', 'desc_img', 'hash_image', 'to_path',
                     'sleep', 'wait_for'}
        found = set()
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr in forbidden:
                    found.add(f.attr)
                elif isinstance(f, ast.Name) and f.id in forbidden:
                    found.add(f.id)
        self.assertEqual(found, set(), f"钩子里出现了阻塞调用: {found}")
