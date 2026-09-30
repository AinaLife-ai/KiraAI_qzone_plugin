"""silent 模式守卫的回归守卫（对应 v1.4.10 的吞消息修复）。

背景（真实故障，2026-09-30）：
    silent 模式原来在 AFTER_XML_PARSE 里无条件 `actions.clear()`。而框架
    `send_xml_messages` 是唯一发送入口、解析后没有任何撤回/补发通道 ——
    清空 = 这一轮模型产出的**全部**消息永久丢失。

    定时发布常要跑几分钟，期间 Midflight 会把群友插话注入同一轮的 tool_result，
    模型按引导语产出的回应也落在同一批 actions 里 → 被一起吞掉。
    这就是用户报的「明明没发空间，消息也消失了」。

判据（日志级）：被清空的轮次，框架打印的 message_id 是**空**的；
正常发送的轮次带真实 message_id。
"""
import asyncio

import _bootstrap as B

main = B.main
QzonePlugin = main.QzonePlugin


def _mk_chain(*texts):
    chain = B.MessageChain()
    for t in texts:
        chain.append(B.Text(t))
    return chain


async def _run_guard(style, is_task, chains, plugin=None):
    """直接驱动真实守卫函数。"""
    plug = plugin or QzonePlugin.__new__(QzonePlugin)
    if plugin is None:
        plug.task_message_style = style
    msg = B.KiraIMMessage(
        sender=B.User(user_id="system_qzone_task" if is_task else "12345"),
        extra={"qzone_task": True} if is_task else None,
    )
    event = B.KiraMessageBatchEvent(session=B.Session(session_id="427674145"),
                                    messages=[msg])
    actions = list(chains)
    await QzonePlugin._silent_task_guard(plug, event, actions)
    return actions


class TestSilentGuardNeverSwallows(B.LoopTestCase):
    def test_task_round_keeps_interjection_replies(self):
        """★ 核心回归：定时任务轮里回应插话的回复必须发得出去。"""
        chains = [_mk_chain("[Reply] Neuro系是标杆"), _mk_chain("人格一致性拉满"),
                  _mk_chain("火到连'我存在吗'都能成梗")]
        out = self.run_(_run_guard("silent", True, chains))
        self.assertEqual(len(out), 3, "定时任务轮里的插话回复不允许被清空")

    def test_never_empties_the_whole_batch(self):
        """★ 绝对安全闸：本批全是旁白时也不许压空（宁可漏一句，绝不吞消息）。"""
        chains = [_mk_chain("【定时任务】说说发布成功")]
        out = self.run_(_run_guard("silent", True, chains))
        self.assertEqual(len(out), 1, "整批压空 = 吞消息，安全策略下必须保留")

    def test_suppresses_narration_but_keeps_replies(self):
        """旁白被压，插话回复保留。"""
        chains = [_mk_chain("[Reply] 回应插话"),
                  _mk_chain("【定时任务】说说已发布，我去看评论了"),
                  _mk_chain("继续聊两句")]
        out = self.run_(_run_guard("silent", True, chains))
        self.assertEqual(len(out), 2)
        joined = " ".join(e.text for c in out for e in c if isinstance(e, B.Text))
        self.assertNotIn("说说已发布", joined, "任务旁白应被压制")
        self.assertIn("回应插话", joined)

    def test_non_task_round_untouched(self):
        """非定时任务轮：守卫完全不介入（绝不误伤普通聊天）。"""
        chains = [_mk_chain("【定时任务】随便说说"), _mk_chain("普通消息")]
        out = self.run_(_run_guard("silent", False, chains))
        self.assertEqual(len(out), 2, "普通轮不允许被 silent 守卫碰")

    def test_notify_mode_untouched(self):
        chains = [_mk_chain("【定时任务】说说已发布")]
        out = self.run_(_run_guard("notify", True, chains))
        self.assertEqual(len(out), 1, "notify 模式守卫不该介入")

    def test_chain_with_non_text_element_is_never_suppressed(self):
        """带 reply / 非文本元素的链即使含旁白关键词也不压（那是明确对话）。"""
        chain = B.MessageChain()
        chain.append(B.Text("【定时任务】说说已发布"))
        chain.append(object())          # 代指 reply / at / image 等非文本元素
        out = self.run_(_run_guard("silent", True, [_mk_chain("另一条"), chain]))
        self.assertEqual(len(out), 2)

    def test_exception_falls_back_to_no_suppression(self):
        """守卫内部一旦异常，绝不压制任何消息（fail-open，杜绝吞消息）。"""
        plug = QzonePlugin.__new__(QzonePlugin)
        plug.task_message_style = "silent"

        class Boom:
            def __contains__(self, item):
                raise RuntimeError("boom")

        plug.TASK_NARRATION_KEYWORDS = Boom()
        chains = [_mk_chain("[Reply] 重要回复"), _mk_chain("【定时任务】说说发布成功")]
        out = self.run_(_run_guard("silent", True, chains, plugin=plug))
        self.assertEqual(len(out), 2, "异常时必须 fail-open（宁可多发也不吞）")


class TestSilentContractIsUpstream(B.LoopTestCase):
    def test_prompt_tells_model_to_stay_silent(self):
        """silent 轮次的指令必须写明"不要发群消息"。

        这是釜底抽薪：框架原生支持 `<msg/>`（不发送任何消息），
        真无痕应该在提示词层达成，而不是靠事后清空 actions。
        """
        import inspect
        src = inspect.getsource(QzonePlugin._send_task_instruction)
        self.assertIn("静默执行", src, "指令里必须带静默约定")
        self.assertIn("<msg/>", src, "应引导模型使用框架原生的静默语法")


class TestNarrationKeywords(B.LoopTestCase):
    def test_keywords_are_class_level(self):
        """关键词挂类属性，便于断言与覆盖。"""
        self.assertTrue(hasattr(QzonePlugin, "TASK_NARRATION_KEYWORDS"))
        self.assertGreaterEqual(len(QzonePlugin.TASK_NARRATION_KEYWORDS), 5)

    def test_keywords_are_task_specific(self):
        """关键词必须是只可能出现在任务旁白里的强特征词，不能误伤普通聊天。"""
        weak = {"说说", "自动", "任务", "发布", "评论"}
        for kw in QzonePlugin.TASK_NARRATION_KEYWORDS:
            self.assertNotIn(kw, weak, f"关键词 {kw!r} 过于宽泛，会误伤普通聊天")


class TestMessageIdIsNotEvidence(B.LoopTestCase):
    """★ 用 message_id 判断"有没有被吞"是**错的** —— 把它钉死在测试里。

    真实行为（框架 core/message_manager.py::_add_message_ids）::

        for i, msg in enumerate(root.findall("msg")):
            if i < len(message_results):
                msg.set("message_id", message_id)
            # ← 没有 else 分支去删除模型自己写的属性

    模型常常模仿上文格式（或幻觉）自己写 message_id。此时即使 actions 被清空
    （一条都没发），日志里**照样**显示一串 id —— 看着像发出去，实际没有。

    用户实测：被吞的那一轮 message_id 是**非空**的。因此判据必须换成
    ON_MESSAGE_SENT / 客户端对质，不能看 message_id。
    """

    def test_documented_criterion_must_not_be_msgid(self):
        """守卫的 docstring 不得再把 message_id 当作判据。"""
        import inspect
        src = inspect.getsource(QzonePlugin._silent_task_guard)
        self.assertIn("修正版", src, "必须写明旧判据已修正")
        self.assertIn("ON_MESSAGE_SENT", src, "必须给出可靠判据")
        self.assertNotIn("被清空的轮次，框架发出的 message_id 是**空**的", src,
                         "旧的错误判据必须删除")

    def test_real_framework_keeps_model_written_ids(self):
        """跑真实框架的 _add_message_ids：清空时模型自写 id 被原样保留。"""
        try:
            from core.message_manager import MessageProcessor
            from core.chat.message_utils import KiraIMSentResult
        except Exception as exc:            # 离线环境无框架时跳过
            self.skipTest(f"无框架源码，跳过: {exc}")

        xml = ('<msg message_id="1587130307">\n'
               '    <text>Neuro系是标杆</text>\n'
               '</msg>')
        # message_results = [] 表示"一条都没发出去"
        out = MessageProcessor._add_message_ids(xml, [])
        self.assertIn('message_id="1587130307"', out,
                      "模型自写的 id 会残留 —— 这正是误判的来源")
