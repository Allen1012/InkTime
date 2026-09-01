"""旁白正文提取的格式容错测试。

存在的意义：这条路径的失败方式是**静默写入垃圾**，而不是报错。提取逻辑一旦对某种
非预期格式退化成「原样当纯文本返回」，数据库里就会出现一条看起来合法的旁白，
上层的空值校验也拦不住，只有人眼看到照片墙上那句话才会发现。

线上真实踩过两次，都收录成了用例：
- 正文只有一个残缺的代码围栏 ``` ，被当成三字符旁白入库；
- 模型在 `json_schema` 下把对象又包了一层数组，整段 JSON 文本被当旁白入库。
"""

from __future__ import annotations

import unittest

from src.analysis.analyze_photos_docker import _extract_caption


class NarrationExtractionTestCase(unittest.TestCase):
    """验证各种正文形态都能取到旁白或被正确判失败。"""

    def extract(self, content) -> str | None:
        """按聊天补全的 message 字典形态调用提取逻辑。"""
        return _extract_caption({"content": content})

    # ------------------------------------------------------------ 应当取到旁白

    def test_plain_text_is_accepted(self) -> None:
        """未启用结构化输出时模型直接返回一句话，保持原有行为。"""
        self.assertEqual(
            "骑行长桥下，蓝天青山伴途。", self.extract("骑行长桥下，蓝天青山伴途。")
        )

    def test_json_object_is_parsed(self) -> None:
        """结构化输出的标准形态：单个对象。"""
        self.assertEqual("青山连绵", self.extract('{"caption": "青山连绵"}'))

    def test_json_array_wrapping_one_object_is_parsed(self) -> None:
        """实测 qwen3.8-max 会把 schema 要求的对象再包一层数组。

        这是线上旁白被写成整段 JSON 文本的直接原因：提取逻辑当时只认 `{` 开头。
        """
        self.assertEqual(
            "骑手穿行在高架桥下的山间公路",
            self.extract('[\n  {\n    "caption": "骑手穿行在高架桥下的山间公路"\n  }\n]'),
        )

    def test_code_fenced_json_object_is_parsed(self) -> None:
        """部分模型仍把 JSON 放进 ```json 围栏里。"""
        self.assertEqual("山路很长", self.extract('```json\n{"caption": "山路很长"}\n```'))

    def test_code_fenced_json_array_is_parsed(self) -> None:
        """围栏与数组两种偏离同时出现时也要能取到。"""
        self.assertEqual("桥下有风", self.extract('```json\n[{"caption": "桥下有风"}]\n```'))

    def test_code_fenced_plain_text_is_parsed(self) -> None:
        """围栏里是纯文本而非 JSON 时，剥掉围栏后按纯文本处理。"""
        self.assertEqual("蓝天很近", self.extract("```\n蓝天很近\n```"))

    def test_surrounding_quotes_are_stripped(self) -> None:
        """模型常给文案加引号，与既有行为一致地剥掉。"""
        self.assertEqual("风从桥下过", self.extract('"风从桥下过"'))

    # ------------------------------------------------------------ 应当判为失败

    def test_bare_code_fence_is_rejected(self) -> None:
        """线上实际写进库的坏值：正文只有一个残缺围栏。

        它不以 `{` 或 `[` 开头，早先会被当成三字符的「合法」旁白入库。
        """
        self.assertIsNone(self.extract("```"))

    def test_empty_code_fence_is_rejected(self) -> None:
        """围栏内没有任何内容同样判失败。"""
        self.assertIsNone(self.extract("```json\n\n```"))

    def test_truncated_json_is_rejected(self) -> None:
        """被 max_tokens 截断的残缺 JSON 不能退化成纯文本。"""
        self.assertIsNone(self.extract("{"))
        self.assertIsNone(self.extract('{"caption":"半句'))
        self.assertIsNone(self.extract("["))

    def test_multi_element_array_is_rejected(self) -> None:
        """数组里多于一个元素说明模型没按「只输出一句」执行，结果不可信。"""
        self.assertIsNone(self.extract('[{"caption": "甲"}, {"caption": "乙"}]'))

    def test_json_without_caption_field_is_rejected(self) -> None:
        """解析成功但取不到 caption 时判失败，不回退成整段 JSON 文本。"""
        self.assertIsNone(self.extract('{"text": "不是 caption 字段"}'))

    def test_blank_caption_is_rejected(self) -> None:
        """caption 为空串或纯空白都不是有效旁白。"""
        self.assertIsNone(self.extract('{"caption": ""}'))
        self.assertIsNone(self.extract('{"caption": "   "}'))

    def test_placeholder_literals_are_rejected(self) -> None:
        """模型把「没有内容」表达成字面量时必须挡掉。"""
        for literal in ("None", "null", "N/A", "无", "暂无"):
            with self.subTest(literal=literal):
                self.assertIsNone(self.extract(literal))

    def test_missing_or_non_string_content_is_rejected(self) -> None:
        """正文缺失、为空或不是字符串时判失败。"""
        self.assertIsNone(self.extract(None))
        self.assertIsNone(self.extract(""))
        self.assertIsNone(self.extract("   "))
        self.assertIsNone(self.extract(123))
        self.assertIsNone(_extract_caption({}))


if __name__ == "__main__":
    unittest.main()
