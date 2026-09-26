"""存量元数据回填生成路径日期候选且保护手工日期的回归测试。"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import backfill_metadata
from src.photo_datetime import PATH_DATETIME_CANDIDATE_KEY
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase


class BackfillMetadataPathCandidateTestCase(TemporaryDatabaseTestCase):
    """验证回填只保存待确认候选，不直接填写拍摄日期。"""

    def _insert_photo(
        self,
        relative_path: str,
        *,
        date_taken: str | None = None,
        date_source: str = "none",
        exif_json: str = "{}",
    ) -> int:
        """创建位于指定相对路径的存量照片记录。"""
        path = self.image_directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-real-jpeg")
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,original_filename,type,exif_datetime,"
                "date_source,exif_json,analysis_status,is_included,is_deleted,created_at,"
                "updated_at,version) VALUES (?,?,'日常',?,?,?,'succeeded',1,0,?,?,1)",
                (
                    str(path),
                    path.name,
                    date_taken,
                    date_source,
                    exif_json,
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                ),
            )
            return int(cursor.lastrowid)

    def _run(self, *, apply: bool) -> None:
        """在临时路径执行回填；默认预览与显式写入都可验证。"""
        arguments = ["backfill_metadata.py"]
        if apply:
            arguments.append("--apply")
        with (
            patch.object(backfill_metadata.a, "DB_PATH", self.database_path),
            patch.object(
                backfill_metadata.a, "IMAGE_DIRS", (self.image_directory,)
            ),
            patch.object(backfill_metadata.a, "read_exif", return_value={}),
            patch("sys.argv", arguments),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, backfill_metadata.main())

    def test_preview_reports_without_writing_candidate(self) -> None:
        """默认预览即使识别出路径日期，也不能修改数据库。"""
        photo_id = self._insert_photo("trip-2024-08-15/preview.jpg")

        self._run(apply=False)

        photo = self.read_photo(photo_id)
        self.assertIsNone(photo["exif_datetime"])
        self.assertEqual({}, json.loads(photo["exif_json"]))

    def test_apply_saves_candidate_without_filling_date(self) -> None:
        """路径日期应进入元数据候选，正式日期和来源仍保持缺失。"""
        photo_id = self._insert_photo("trip-2024-08-15/a.jpg")

        self._run(apply=True)

        photo = self.read_photo(photo_id)
        self.assertIsNone(photo["exif_datetime"])
        self.assertEqual("none", photo["date_source"])
        self.assertEqual(
            "2024:08:15 00:00:00",
            json.loads(photo["exif_json"])[PATH_DATETIME_CANDIDATE_KEY],
        )

    def test_apply_preserves_manual_date_and_removes_stale_candidate(self) -> None:
        """管理员确认过的日期不重算，历史遗留候选应从元数据清除。"""
        metadata = {
            "datetime": "2020:01:02 03:04:05",
            "date_source": "manual",
            PATH_DATETIME_CANDIDATE_KEY: "2024:08:15 00:00:00",
        }
        photo_id = self._insert_photo(
            "trip-2024-08-15/manual.jpg",
            date_taken="2020:01:02 03:04:05",
            date_source="manual",
            exif_json=json.dumps(metadata),
        )

        self._run(apply=True)

        photo = self.read_photo(photo_id)
        self.assertEqual("2020:01:02 03:04:05", photo["exif_datetime"])
        self.assertEqual("manual", photo["date_source"])
        self.assertNotIn(
            PATH_DATETIME_CANDIDATE_KEY, json.loads(photo["exif_json"])
        )
