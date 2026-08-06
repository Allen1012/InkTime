# 05 - Web 服务器与前端

## 概述

Web 服务器基于 Flask，提供三大功能：
1. ESP32 固件下载接口
2. 照片管理 WebUI（可关闭）
3. 沉浸式纯展示页面

**核心文件**：`src/server/server.py`

## 服务器配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| FLASK_HOST | 0.0.0.0 | 监听地址 |
| FLASK_PORT | 5005 | 监听端口 |
| DOWNLOAD_KEY | inktime | ESP32 下载路径密钥 |
| ENABLE_REVIEW_WEBUI | True | 是否开启 WebUI |
| DAILY_PHOTO_QUANTITY | 5 | 每日照片数量 |

## API 接口

### ESP32 下载接口

| 端点 | 说明 |
|------|------|
| `GET /static/inktime/{key}/photo_{idx}.bin` | 下载第 idx 张渲染照片 |
| `GET /static/inktime/{key}/latest.bin` | 下载最新渲染照片 |
| `GET /static/inktime/{key}/preview.png` | 下载预览图 |

`{key}` 必须匹配 DOWNLOAD_KEY，否则返回 404。

### 照片 API

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/api/photos` | GET | page, filter, sort, limit | 照片列表（分页） |
| `/api/photo/<id>` | GET | — | 照片详情 |
| `/api/photo/thumbnail` | GET | path | 缩略图（300×200） |
| `/api/photo/full` | GET | path | 原图 |
| `/api/search` | GET | q, page, limit | 搜索（caption/side_caption/path） |
| `/api/category/stats` | GET | — | 分类统计 |
| `/api/category/photos` | GET | category, page, limit | 分类照片 |
| `/api/md_list` | GET | — | 所有存在的 MM-DD 列表 |
| `/api/random_day` | GET | — | 随机一天 |

### 排序选项

- `latest` — 按拍摄时间倒序
- `oldest` — 按拍摄时间正序
- `memory` — 按回忆度倒序
- `beauty` — 按美观度倒序

### 筛选选项

- `all` — 全部
- 按 type 字段模糊匹配（如 `人物`、`风景`）

## 前端页面

### 页面路由

| 路由 | 页面 | 说明 |
|------|------|------|
| `/` | index.html | 相册首页（照片网格） |
| `/photo/<id>` | photo.html | 照片详情页 |
| `/category` | category.html | 分类浏览 |
| `/search?q=` | search.html | 搜索结果 |
| `/display` | display.html | 沉浸式展示页面 |
| `/display/<id>` | display.html | 指定照片展示 |
| `/files/` | 动态生成 | 输出目录浏览 |

### 技术栈

- Bootstrap 5.3 — 响应式布局
- Font Awesome 6.4 — 图标
- 原生 JavaScript (ES6+) — 交互逻辑
- Jinja2 — 模板引擎

### 纯展示页面（/display）

设计理念：极简、沉浸式，类似实体电子相册。

功能：
- 全屏照片展示，深色背景
- 底部半透明信息栏：文案 + 日期 + 地点
- 自动切换（默认 10 秒间隔）
- 手动切换：左右箭头 / 屏幕边缘点击 / 触摸滑动 / 键盘方向键
- 点击照片跳转详情页

### 响应式断点

| 断点 | 屏幕宽度 | 照片网格列数 |
|------|---------|-------------|
| xs | < 576px | 1 列 |
| sm | 576-767px | 2 列 |
| md | 768-991px | 3 列 |
| lg | 992-1199px | 4 列 |
| xl | ≥ 1200px | 5 列 |

## 安全考虑

- DOWNLOAD_KEY 作为简单路径口令（非加密），防止随意访问
- 公网部署建议加 HTTPS/反代鉴权或限制内网访问
- 文件浏览接口有路径穿越防护（`_safe_join`）
- WebUI 可通过 ENABLE_REVIEW_WEBUI 关闭
