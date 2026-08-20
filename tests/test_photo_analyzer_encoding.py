#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""校验单张分析只对照片编码一次，且独立旁白任务仍能自行编码。

评分与旁白是两次独立模型调用，但发送的是同一份图片。这里锁死三条：编码只发生
一次、两次调用收到的是同一份结果、只重写旁白的任务不依赖调用方预先编码。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from src.analysis import analyze_photos_docker as legacy
from src.analysis import photo_analyzer


class SharedImageEncodingTestCase(unittest.TestCase):
    """用打桩替换模型调用，只观察编码次数与参数传递。"""

    def setUp(self) -> None:
        """记录并在用例结束后恢复被替换的旧模块函数。"""
        self.encode_calls = 0
        self.vlm_received: list[str | None] = []
        self.caption_received: list[str | None] = []
        for name in (
            "encode_image_to_b64",
            "call_vlm",
            "generate_side_caption",
            "resolve_datetime",
        ):
            self.addCleanup(setattr, legacy, name, getattr(legacy, name))

        def fake_encode(path: Path) -> str:
            self.encode_calls += 1
            return f"B64:{Path(path).name}"

        def fake_call_vlm(path: Path, image_b64: str | None = None) -> tuple:
            self.vlm_received.append(image_b64)
            return (
                {
                    "caption": "画面描述",
                    "type": "日常",
                    "memory_score": 70.0,
                    "beauty_score": 60.0,
                    "reason": "理由",
                },
                {"datetime": None, "width": 100, "height": 200},
            )

        def fake_caption(path: Path, image_b64: str | None = None) -> str:
            self.caption_received.append(image_b64)
            return "一句旁白"

        legacy.encode_image_to_b64 = fake_encode
        legacy.call_vlm = fake_call_vlm
        legacy.generate_side_caption = fake_caption
        legacy.resolve_datetime = lambda path, value, original_filename=None: (
            None,
            "none",
        )

    def test_single_photo_analysis_encodes_once(self) -> None:
        """验证评分与旁白共用一次编码结果。"""
        result: dict[str, Any] = photo_analyzer.analyze_single_photo(
            Path("/tmp/sample.jpg"), city_resolver=lambda lat, lon: ""
        )

        self.assertEqual(1, self.encode_calls)
        self.assertEqual(["B64:sample.jpg"], self.vlm_received)
        self.assertEqual(["B64:sample.jpg"], self.caption_received)
        self.assertEqual("一句旁白", result["side_caption"])
        self.assertEqual(70.0, result["memory_score"])

    def test_narration_only_job_encodes_by_itself(self) -> None:
        """验证只重写旁白时不要求调用方预先编码。"""
        narration = photo_analyzer.generate_narration(Path("/tmp/sample.jpg"))

        self.assertEqual("一句旁白", narration)
        self.assertEqual(0, self.encode_calls)
        self.assertEqual([None], self.caption_received)

    def test_encoding_failure_fails_before_model_calls(self) -> None:
        """验证编码失败时不发起任何模型调用，避免部分写入。"""

        def failing_encode(path: Path) -> str:
            raise OSError("broken file")

        legacy.encode_image_to_b64 = failing_encode

        with self.assertRaises(RuntimeError) as caught:
            photo_analyzer.analyze_single_photo(Path("/tmp/sample.jpg"))

        self.assertIn("读取图片失败", str(caught.exception))
        self.assertEqual([], self.vlm_received)
        self.assertEqual([], self.caption_received)


if __name__ == "__main__":
    unittest.main()
