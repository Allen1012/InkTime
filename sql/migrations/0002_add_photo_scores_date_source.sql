-- 标记 exif_datetime 的来源；迁移器会采纳迁移体系引入前已存在的同名字段。
ALTER TABLE photo_scores ADD COLUMN date_source TEXT
