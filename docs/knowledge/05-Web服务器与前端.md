# 05 - Web 服务器与前端

## 概述

Web 服务器基于 Flask，提供三大功能：
1. ESP32 固件下载接口
2. 照片管理 WebUI（可关闭）
3. 沉浸式纯展示页面

**核心文件**：`src/server/app.py`（应用工厂）、`src/server/blueprints/`（路由）、
`src/server/services.py`（业务编排）、`src/server/repositories/photo_repository.py`（照片查询）。
`src/server/server.py` 只保留旧启动路径兼容入口。

### 分层与依赖方向

```text
Flask app.py
  → Blueprint（解析请求、转换响应）
    → Service（业务行为与序列化）
      → Repository（参数化 SQL）
        → src.database（统一 SQLite 连接）
```

公开 Blueprint 保持改造前全部 27 条 URL 与 HTTP 方法不变；后台页面 Blueprint 使用
`/admin`，后台接口 Blueprint 使用 `/api/admin`。阶段 2 已接入 Flask-Login 和 Flask-WTF：

- `GET/POST /admin/login` 显示或提交登录表单，失败统一提示，不区分账号不存在、密码错误、停用或限流；已限流请求在查询管理员和计算密码哈希前直接返回，未限流且用户名不存在时仍执行 dummy hash；
- `POST /admin/logout` 仅接受带跨站请求伪造 token 的表单，不提供 GET 退出；
- `GET /admin` 和未来新增的 `/admin/*` 页面由 Blueprint `before_request` 默认要求认证；
- `GET /api/admin` 和未来新增的 `/api/admin/*` 接口由另一个 Blueprint `before_request` 默认要求认证；
- 匿名页面请求重定向到 `/admin/login?next=...`，`next` 仅接受以单个 `/` 开头、无 scheme/netloc 的站内相对路径；
- 匿名后台接口统一返回 HTTP 401 JSON；登录后 `/api/admin` 仅返回 `phase=2`、`authentication=implemented` 和当前用户名；
- 公开 Blueprint 整体精确豁免跨站请求伪造校验，兼容现有 `POST /api/render`、`POST /api/settings`；后台登录、退出和未来写接口均校验 token。

后台页面使用 `templates/admin/base.html`、`login.html`、`index.html` 与本地
`static/css/admin.css`，不依赖外部内容分发网络。认证实现集中在 `auth.py`、`forms.py` 和
`repositories/admin_user_repository.py`，不复用公开的 `POST /api/settings` 模拟接口。

### 公开前端动态内容安全

公开相册首页、分类页和照片详情页把接口返回的照片路径、旁白、分类、位置、相机信息及可交换图像文件格式元数据统一视为不可信数据。`main.js`、`category.js` 和 `photo.js` 使用 `createElement`、`textContent`、`dataset` 与显式属性赋值构建页面，禁止把这些字段拼接到 `innerHTML`。照片编号必须先规范化为正整数，评分必须转换为有限数并夹在 0 至 100，缩略图和原图地址只接受同源的 `/api/photo/thumbnail` 与 `/api/photo/full` 路径；非法地址回退到占位图。提示消息同样使用文本节点和独立关闭按钮，避免未来把接口错误文案接入时重新形成持久化跨站脚本入口。

固定分页模板不承载接口字段，可以继续使用静态结构；新增或修改动态页面时，不得用“通用 HTML 转义后继续拼接字符串”代替文档对象模型构建，因为文本、URL、样式和属性属于不同安全上下文。

### 阶段 3 只读照片后台

阶段 3 在现有认证边界内增加以下页面，全部使用服务端 Jinja 渲染，不新增后台写接口：

| 路由 | 页面能力 |
|------|----------|
| `GET /admin` | 照片总数、平均评分、拍摄时间覆盖、城市覆盖和分类数量；每个统计独立捕获 SQLite 异常并降级 |
| `GET /admin/photos` | 服务端分页、网格/表格切换、缩略图懒加载、搜索、分类、拍摄日期和排序 |
| `GET /admin/photos/<id>` | 照片、文案、评分、相机与可交换图像文件格式元数据只读详情；原文件缺失时仍展示数据库记录 |
| `GET /admin/jobs` | 后台任务列表；阶段 5 起已接入照片任务，阶段 6 起合并展示维护任务 |

公开相册 WebUI、`GET /api/photos` 等公开接口无需管理员登录；`/admin/*` 和 `/api/admin/*` 统一要求管理员登录，后台写请求还必须通过跨站请求伪造保护。公开与管理员边界按 Blueprint 划分，文档必须分别说明两类访问控制。

后台照片查询由独立 `AdminPhotoService` 编排，继续复用 `PhotoRepository` 和
`MediaService`；公开 `PhotoService` 的字段与分页契约不变。后台列表默认每页 24 条，最大
100 条，排序表达式只接受服务端白名单；搜索覆盖照片路径、描述、旁白与城市，并转义
`%`、`_` 等 SQL `LIKE` 通配符。后台列表和详情使用认证保护、按照片编号定位的媒体路由，
只允许读取 `is_deleted=0` 的活动记录，但不按分析状态过滤，因此管理员仍可查看并处理
pending、running 或 failed 照片。公开缩略图和原图接口继续只允许 legacy 或 succeeded
照片，管理端放宽不会扩大匿名访问范围；所有文件读取仍要求路径位于 `IMAGE_DIR` 且不在
`.trash` 目录。

### 阶段 4 照片编辑与批量操作

阶段 4 在阶段 3 查询页面上增加受控写能力，并继续复用后台认证与跨站请求伪造保护：

| 路由 | 页面或接口能力 |
|------|----------------|
| `GET/POST /admin/photos/<id>` | 展示并编辑描述、旁白、评分理由、城市、分类、拍摄日期时间和分析状态；提交携带版本号 |
| `POST /admin/photos/batch` | 页面通过独立按钮将 1 至 100 张勾选照片逐项安全移入回收站，也支持批量设置分类或分析状态；单项失败不影响其他照片 |
| `PATCH /api/admin/photos/<id>` | JSON 单照片编辑接口，版本冲突返回 HTTP 409 |
| `POST /api/admin/photos/batch` | JSON 批量接口，每批 1 至 100 项 |

`AdminPhotoManagementService` 统一执行字段长度、分类和状态白名单校验。分类允许自定义，使用
`/` 分隔，最多 10 个标签，每个最多 30 个字符；描述、旁白、评分理由和城市上限分别为
500、100、1000 和 100 个字符。日期支持完整日期时间，也支持只提交日期并保留原时分秒；
人工日期提交会在同一事务同步 `exif_datetime`、`date_source=manual`、
`exif_json.datetime`、`exif_json.DateTime` 和 `exif_json.date_source`。

所有照片编辑使用 `version` 乐观锁。更新 SQL 仅在 `id` 和预期版本同时匹配时生效，成功后
`version + 1`；版本变化返回 HTTP 409，绝不覆盖其他请求的更新。照片更新和
`photo_audit_log` 审计记录使用同一 `BEGIN IMMEDIATE` 短事务，审计保存管理员编号、用户名
快照、行为、修改前后 JSON、批次编号和协调世界时。

批量请求先做整体格式校验，再在一个短事务中逐项检查照片与版本：不存在和版本冲突只让
对应项目失败，其余项目保留成功；数据库异常则整个批次回滚。磁盘文件缺失只影响文件状态
展示，不等同管理员软删除。阶段 4 仅建立 `is_deleted`、`deleted_at` 生命周期基础字段，
软删除、恢复及媒体访问控制留到阶段 6。

### 阶段 5 上传与任务管理

| 路由 | 方法 | 能力 |
|------|------|------|
| `/admin/photos/upload` | GET | 多文件上传页面 |
| `/admin/jobs` | GET | 类型、状态、进度、尝试、错误和关联照片 |
| `/api/admin/photos/upload` | POST | JPEG/PNG/WebP 上传并排队分析 |
| `/api/admin/jobs` | GET | 任务列表 |
| `/api/admin/jobs/<id>/cancel` | POST | pending 立即取消，running 协作取消 |
| `/api/admin/jobs/<id>/retry` | POST | 未达到三次上限的失败或取消任务重试 |
| `/api/admin/photos/<id>/reanalyze` | POST | 单张重新分析 |
| `/api/admin/photos/reanalyze` | POST | 批量重新分析 |
| `/api/admin/photos/<id>/regenerate-narration` | POST | 重新生成旁白 |

上述页面和接口继续使用后台 Blueprint 的默认登录与跨站请求伪造保护。上传不信任 Content-Type：服务端先完整校验整批 JPEG、PNG 或 WebP，在重编码前保存原始拍摄时间、GPS 与相机字段，在正式年月目录对规范化临时文件执行 `flush + fsync`，并以 `os.replace` 原子发布。`content_sha256` 只计算规范化后正式文件的字节；同批或历史重复内容返回逐项 `duplicate`，其余返回 `accepted` 与计数。任何文件校验失败时整批不落库、不发布，数据库事务失败会补偿删除本批正式文件。

`POST /api/admin/jobs/backfill-content-hash` 可为缺少摘要的历史照片创建低优先级 `backfill_content_hash` 任务，按照片编号稳定扫描并自动跳过已有活跃任务。工作进程流式计算摘要，在取消、停止或高优先级任务出现时主动让出，不调用视觉语言模型。

普通管理员取消运行任务时先写入协作取消标记，由当前工作进程在安全边界终结；任务被取消或租约已过期后不能继续续租。照片软删除不等待外部分析调用返回，而是在软删除事务内直接把该照片全部等待中和运行中任务置为 `canceled`，清除租约并写 `photo_deleted` 事件；分析中的照片同时收口为 `failed/job_canceled`。外部调用晚到的结果因任务终态和照片版本条件无法提交。

### 阶段 6 回收站、永久删除与维护任务

阶段 6 后 `/api/admin` 返回 `phase=6`。所有页面和接口继续继承后台 Blueprint 的认证与跨站请求伪造保护：

| 路由 | 方法 | 能力 |
|------|------|------|
| `/admin/photos/<id>/trash` | POST | 携带预期版本安全移入回收站 |
| `/admin/trash` | GET | 分页显示删除时间、原始位置、操作人和版本 |
| `/admin/trash/<id>/restore` | POST | 不覆盖原位置恢复，冲突返回 HTTP 409 |
| `/admin/trash/<id>/purge` | GET/POST | 独立确认页；确认文本和预期版本双重防误操作 |
| `/admin/trash/cleanup-preview` | GET | 默认 30 天截止条件的只读预览 |
| `/admin/trash/cleanup` | POST | 排队分批过期清理维护任务 |
| `/api/admin/trash` | GET | 回收站分页 JSON |
| `/api/admin/photos/<id>` | DELETE | 设计契约中的软删除接口；请求体携带预期版本 |
| `/api/admin/photos/<id>/trash` | POST | 兼容表单语义的软删除接口别名 |
| `/api/admin/photos/<id>/restore` | POST | 设计契约中的恢复接口 |
| `/api/admin/trash/<id>/restore` | POST | 恢复接口别名 |
| `/api/admin/trash/<id>` | DELETE | 设计契约中的永久删除接口；要求确认文本和预期版本 |
| `/api/admin/trash/<id>/purge` | POST | 永久删除接口别名 |
| `/api/admin/trash/cleanup-preview` | GET | 只读清理预览 JSON |
| `/api/admin/trash/cleanup` | POST | 排队清理 JSON 接口 |

后台任务页合并展示阶段 5 照片任务和阶段 6 维护任务，每行明确 `photo` 或 `maintenance` 队列、
任务类型、状态、结果和安全错误码；新取消与重试 URL 包含队列名，避免编号碰撞，同时保留
阶段 5 的 `/api/admin/jobs/<id>/cancel` 和 `/api/admin/jobs/<id>/retry` 照片任务兼容路径。

公开媒体端点不再仅判断路径位于 `IMAGE_DIR`：解析后先拒绝 `.trash`，再回查该路径必须对应
`is_deleted=0` 且分析状态可展示的数据库记录。`DeviceService` 与 `/files/` 每次读取
`display_artifact_state`；blocked 时返回不存在，直到两套渲染全部发布、删除不在新 manifest 中的旧高编号受管产物并保存新 manifest。
公开列表、搜索、分类、详情、展示选片、随机日期和两套每日渲染均排除删除照片；软删除和恢复
还会立即清空一小时月日缓存。

### 阶段 7 Web 配置管理与展示热更新

阶段 7 的 Web 批次把独立 `ConfigurationService` 注册为应用实例级共享服务。管理员页面、公开设置兼容层、展示模板、展示选片和信息面板在请求时读取同一全局版本；每次读取先轻量检查 `app_settings.version`，版本变化才重载完整 JSON，不再通过 `gallery.configure()` 或 `panel.configure()` 修改请求间共享全局变量。

| 路由 | 方法 | 能力 |
|------|------|------|
| `/admin/settings` | GET/POST | 查看全部注册配置、当前版本和最近审计；批量保存在线配置 |
| `/api/admin/settings` | GET/PATCH | 获取脱敏配置元数据；携带 `expected_version` 原子更新 `changes` |
| `/api/admin/settings/audit` | GET | 按时间倒序读取 1 至 100 条脱敏配置审计 |

这些路由继承管理员 Blueprint 的登录与跨站请求伪造保护。页面和接口只允许修改注册表中 `editable=true`、`restart_required=false` 且非敏感的配置；未知键、类型错误、非法枚举、越界值或只读项会使整批零写入。提交使用全局版本乐观锁，旧版本返回 HTTP 409；配置、版本和审计处于同一事务。全部值未变化时不递增版本，也不产生空审计。

敏感配置只显示是否已配置，真实值不进入页面、接口、审计或任务快照。公开 `GET /api/settings` 保持原裸 JSON 字段，并按请求读取展示轮换配置；公开 `POST /api/settings` 仍是无需认证的兼容模拟响应，不会修改真实配置。

请求期热更新覆盖 `DISPLAY_TEMPLATE`、`DISPLAY_ROTATE_MODE`、`DISPLAY_ROTATE_INTERVAL_SEC`、`DISPLAY_KEEP_AWAKE`、`DISPLAY_UI_HIDE_DELAY_SEC`、`DISPLAY_MIN_SCORE`、`DISPLAY_NEW_PHOTO_WEIGHT`、`ONTHISDAY_COUNT`、`ONTHISDAY_STRATEGY`、`ONTHISDAY_MIN_YEAR` 和 `PANEL_AI_MODEL`。展示选片阈值与权重作为单次调用参数贯穿算法；信息面板条数、策略、年份和模型也显式贯穿筛选链路，人工智能结果缓存键包含实际模型，切换模型不会继续命中旧模型结果。

分析、渲染和工作进程已接入任务配置快照：独立工作进程在任务首次认领的同一事务中按任务类型固化 `analysis`、`render` 或 `worker` 作用域快照；租约恢复、自动重试和人工重试继续沿用原快照。

## 服务器配置

配置仍以环境变量 / `.env` 为部署来源（`config/config.py` 已废弃）。
`create_app(config_overrides=None)` 在函数内加载配置，再应用可选覆盖；覆盖主要用于指向
临时数据库的验证，不修改环境变量。路径值统一按项目根目录解析后写入 `app.config`。
`APP_ENV` 只允许 `development`、`testing`、`production`；systemd 和 Docker 的 Web 服务
部署文件强制使用 `production`。生产模式必须显式提供非空随机 `SECRET_KEY`，且
`SESSION_COOKIE_SECURE` 必须为 `True`，任何不安全值都会让应用拒绝启动。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| FLASK_HOST | 0.0.0.0 | 监听地址 |
| FLASK_PORT | 5005 | 监听端口（本机部署实际用 8888） |
| DB_PATH | ./data/photos.db | 数据库路径 |
| IMAGE_DIR | ./data/photos | 相册目录，同时是照片读取接口的安全边界 |
| BIN_OUTPUT_DIR | ./data/output | 渲染产物目录 |
| DOWNLOAD_KEY | inktime | ESP32 下载路径密钥 |
| ENABLE_REVIEW_WEBUI | True | 是否开启 WebUI |
| ENABLE_FILE_BROWSER | False | 是否开放 `/files/` 目录浏览，默认关闭 |
| DAILY_PHOTO_QUANTITY | 5 | 每日照片数量 |
| PROJECT_NAME | InkTime 相册 | 网站显示名，值含空格需在 `.env` 中加引号 |
| DISPLAY_ROTATE_MODE | interval | 展示页自动切换模式：interval / hourly / minutely / daily / off，非法值回退 interval 并告警 |
| DISPLAY_ROTATE_INTERVAL_SEC | 60 | interval 模式的切换间隔（秒），最小 1 |
| DISPLAY_KEEP_AWAKE | True | 展示页是否请求 Screen Wake Lock 阻止空闲息屏/锁屏 |
| DISPLAY_UI_HIDE_DELAY_SEC | 3 | 静置多少秒后自动隐藏操作界面，0 表示不隐藏 |
| DISPLAY_TEMPLATE | classic | 展示页模板：classic / dashboard，非法值回退 classic 并告警 |
| ONTHISDAY_COUNT | 2 | 「历史上的今天」展示条数 |
| ONTHISDAY_STRATEGY | curated | 筛选策略：recent / curated / ai |
| ONTHISDAY_MIN_YEAR | 1900 | curated 策略的年份下限 |
| PANEL_AI_MODEL | （空） | ai 策略使用的模型，留空回退 MODEL_NAME |
| DISPLAY_MIN_SCORE | 70 | 展示页候选池的 memory_score 下限，0 表示全部 |
| DISPLAY_NEW_PHOTO_WEIGHT | 3 | 未展示过的照片在同轮内的权重倍数，1 为完全公平 |

## 启动方式

`src/server/app.py` 提供真正应用工厂：每次 `create_app()` 都创建新的 Flask 实例，
显式保持 `src/server/templates`、`src/server/static` 和应用级 `static` 端点。
导入模块不会创建 Flask 应用或输出目录；目录创建、gallery/panel 配置、Service 与
Blueprint 注册都在工厂调用期间完成。

```bash
# 统一生产入口（本地、systemd、Docker）
./venv/bin/python -m src.server.run_server

# 兼容旧部署方式
./venv/bin/waitress-serve --call src.server.server:create_app

# 开发（Flask 内置服务器，仅本地调试）
./venv/bin/python src/server/server.py
```

`run_server.py` 先创建应用，再从 `application.config` 读取 `FLASK_HOST` 和
`FLASK_PORT`。`server.py` 只重导出 `create_app` 并保留直接执行兼容入口。生产 Web 服务与独立工作进程启动时只调用当前结构断言，不执行迁移；部署应先显式运行 `scripts/database_admin.py migrate`，再以只读 `check-schema` 门禁确认目标版本 45。systemd 和 Docker Compose 都要求结构门禁成功后才启动 Web 服务与后台工作进程。


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
| `/api/panel` | GET | force | 信息面板聚合数据（日期 / 农历节气 / 历史上的今天），force=1 跳过缓存 |
| `/api/display/next` | GET | exclude | 按轮次算法取下一张照片并记账，exclude 传当前 id 避免连续重复 |
| `/api/display/stats` | GET | — | 展示次数分布与轮次进度 |
| `/api/display/prev` | GET | — | 返回 410，「上一张」由前端历史栈实现 |
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
| `/files/` | 动态生成 | 输出目录浏览，默认关闭（`ENABLE_FILE_BROWSER=False` 时返回 404） |

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
- 自动切换，模式可配置（见下）
- 手动切换：左右箭头 / 屏幕边缘点击 / 触摸滑动 / 键盘方向键
- 点击照片跳转详情页

### 自动切换模式

切换行为由 `.env` 配置，`display.js` 启动时从 `/api/settings` 读取：

| 模式 | 行为 |
|---|---|
| `interval` | 每 `DISPLAY_ROTATE_INTERVAL_SEC` 秒切换 |
| `hourly` | 每到整点切换（10:00、11:00…），与真实时钟对齐 |
| `minutely` | 每到整分切换，主要用于快速验证对齐逻辑 |
| `daily` | 每天 00:00 切换一次 |
| `off` | 不自动切换，仅手动 |

实现要点：

- 用**递归 `setTimeout`** 而非 `setInterval`。对齐模式每次都重新计算到下一个时钟
  边界的延迟，避免定时器误差累积导致逐渐偏离整点
- 对齐模式（hourly / minutely / daily）下**手动切换不重置计时**，否则下一次
  切换会偏离时钟边界，失去「整点切换」的意义；`interval` 模式保持原有的重置行为
- 播放按钮的 title 会显示当前模式，可用来确认配置是否生效
- 改了 `.env` 需要重启服务（`sudo systemctl restart inktime-server`）才生效

### 长时间播放不被息屏/锁屏

系统的空闲策略会在若干分钟后息屏或锁屏，打断展示。两个手段，可叠加使用：

**1. Screen Wake Lock（`DISPLAY_KEEP_AWAKE=True`）**

`display.js` 启动时调 `navigator.wakeLock.request('screen')`，底层走系统的
idle inhibit 机制（GNOME 下等价于 `gnome-session-inhibit --inhibit idle`），
属于系统设计支持的行为，不修改任何系统设置。

两个必须知道的限制：

- **要求安全上下文**。必须用 `http://127.0.0.1:<端口>/display` 或 HTTPS 访问；
  通过局域网 IP 的 http 访问时 `navigator.wakeLock` 直接是 `undefined`
- 标签页切到后台时浏览器会自动释放锁，代码在 `visibilitychange` 时重新请求

状态显示在右上角指示器里，与切换模式并排：生效为 `· 常亮`，
失败则显示原因（`· 需用 127.0.0.1 访问` / `· 常亮被拒绝` / `· 常亮已释放`），
避免出现「没生效但不知道为什么」。

**2. `scripts/display_kiosk.sh`（不受安全上下文限制）**

用 `gnome-session-inhibit --inhibit idle` 包装浏览器全屏启动：

```bash
./scripts/display_kiosk.sh                         # 默认 127.0.0.1 + .env 的端口
./scripts/display_kiosk.sh http://10.0.0.5:8888/display
BROWSER=firefox ./scripts/display_kiosk.sh
```

脚本退出（关窗口或 Ctrl+C）时抑制自动解除。走 `127.0.0.1` 时两层同时生效。

> 排查思路：`systemd-inhibit --list` 看抑制是否注册上；
> `dbus-send --session --print-reply --dest=org.gnome.SessionManager /org/gnome/SessionManager org.gnome.SessionManager.IsInhibited uint32:8`
> 返回 `true` 表示 GNOME 侧的 idle 抑制已生效。
> 注意 X11 时代的 `xdotool` 在 Wayland 会话下不可用。

### 操作界面自动隐藏

静置 `DISPLAY_UI_HIDE_DELAY_SEC` 秒后，给 `.display-container` 加 `.ui-hidden`，
由 CSS 淡出右上角指示器与左右切换提示，并把光标设为 `cursor: none`，
让画面只剩照片。鼠标移动 / 按键 / 滚轮 / 触摸任一动作立即恢复并重新计时。

范围与边界：

- **底部文案区（`.info-container`）不隐藏** —— 文案、日期、地点属于照片内容，
  不是操作控件
- 鼠标悬停在右上角指示器上时不隐藏（`uiPinned` 标志），否则正要点暂停按钮时
  界面会消失
- 隐藏态下同时设 `pointer-events: none`，避免点到看不见的元素
- `DISPLAY_UI_HIDE_DELAY_SEC=0` 关闭该行为，负值会被夹到 0

> 实现注意：原本的 `.display-container:hover .navigation-hint { opacity: 1 }`
> 会让左右提示在鼠标停在窗口内时一直显示（`:hover` 持续成立）。
> 规则已改为 `.display-container:not(.ui-hidden):hover`，比加 `!important` 干净。

## 展示页选片逻辑

展示页与墨水屏**是两套完全独立的选片逻辑**：墨水屏走「历史上的今天」
（`render_daily_photo.py`，产物是 `.bin`），展示页走轮次制随机。
两者的展示计数也分开记（`display_stats.channel`），否则会互相打乱轮次。

### 算法（src/server/gallery.py）

```
min_count = 候选池里 show_count 的最小值
从 show_count == min_count 的照片中加权随机选一张，选中后 +1
这批全部 +1 后 min_count 自然上升，新一轮开始
```

一轮内每张照片恰好出现一次（覆盖性），轮内顺序随机（新鲜感），
状态全在库里，进程重启不丢。1000 张 + 整点切换 ≈ 一轮 41 天。

### 新照片为什么不会霸屏

新照片入场时 `show_count` 对齐到**当前 min_count**，并在同层内获得
`DISPLAY_NEW_PHOTO_WEIGHT` 倍权重，因此更早出现但与老照片混排。

> **踩过的坑**：不能改用「层级偏移」给新照片提前量（即把 `show_count` 设成比
> `min_count` 更小）。那样新照片会独占一个更低层级，**必然被连续选完才轮到老照片**，
> 也就是霸屏 —— 实测 1000 张跑 15 轮后加 3 张，前 3 次全是新照片。
> 提前量给多少都一样，只有同层加权才能混排。

实测权重对首次出现位置的影响（200 张池子）：

| 权重 | 新照片首次出现 | 整点切换约合 |
|------|---------------|-------------|
| 1.0（完全公平） | 第 96 次 | 4 天 |
| 3.0（默认） | 第 35 次 | 1.5 天 |
| 8.0 | 第 11 次 | 半天 |

### 其他要点

- **初始化是懒惰的**：新照片在 `display_stats` 中无记录，首次选片时才补基线值。
  因此分析脚本完全不需要感知展示逻辑
- **新照片自动进入候选**：每次请求都实时查库，无需定时拉取列表。
  前端也不再持有全量列表（上千条元数据传给前端本就不合理）
- **「上一张」走前端历史栈**（限长 50），往回翻不请求服务端、不消耗展示次数，
  否则来回翻几次就会打乱轮次统计；翻到栈顶再往前才向服务端要新的
- `/display/<id>` 指定照片也不计数，避免手动查看污染统计
- 候选池不要求有 EXIF 拍摄时间（与墨水屏不同），web 展示不依赖日期

## 展示页模板

`DISPLAY_TEMPLATE` 决定 `/display` 渲染哪套模板，也可用 URL 参数
`/display?template=dashboard` 临时预览，便于对比而不必改配置重启。

| 模板 | 文件 | 布局 |
|------|------|------|
| `classic` | `display.html` | 纯照片全屏（原有布局） |
| `dashboard` | `dashboard.html` + `dashboard.css` + `dashboard.js` | 左侧照片 + 右侧信息栏 |

**关键设计：dashboard 沿用与 classic 完全相同的照片区 DOM id**
（`display-photo` / `display-caption` / `display-date` / `display-location` /
`auto-play-label` 等），因此 `display.js` 无需任何改动即可复用 ——
照片加载、切换、屏幕常亮、界面自动隐藏在两套模板下行为一致。
`dashboard.js` 只负责右侧信息栏，`dashboard.css` 只覆盖布局，
照片区视觉细节继续沿用 `display.css`。

### 右侧信息栏

| 区块 | 数据来源 | 说明 |
|------|---------|------|
| 时钟 | 前端本地时间 | 每秒更新，起始对齐到整秒；等宽数字避免秒数跳动导致宽度抖动 |
| 日期 / 星期 | `/api/panel` | 跨零点时前端检测到日期变化会立即重新拉取 |
| 农历 / 节气 / 干支 / 传统节日 | `/api/panel`（离线计算） | 当日节气与传统节日以徽章显示，并给出距下个节气天数 |
| 历史上的今天 | `/api/panel`（维基） | 条数与筛选策略可配；数据源失败时整块隐藏 |

布局要点：右栏 `justify-content: center` 整体上下居中，因此历史区必须用
`flex: 0 0 auto` 而非 `1 1 auto`，否则它会撑满剩余空间破坏居中效果。

### 「历史上的今天」筛选策略

维基「历史上的今天」的选材本身偏重灾难与战争 —— 实测某日 30 条候选里，
按年份取最近的两条会得到「空难 21 人罹难」和「特大泥石流灾害」。
家庭相框场景不适合天天展示这类内容，因此提供三种策略：

| 策略 | 做法 | 特点 |
|------|------|------|
| `recent` | 纯按年份降序 | 最简单，但内容常偏沉重 |
| `curated` | 过滤负面关键词 + 年份下限 + 周年/长度加分 | 无外部依赖；能挡掉明显灾难，挡不掉枯燥的百科腔 |
| `ai` | 大模型挑选并改写 | 效果最好，失败自动回退 `curated` |

实现要点：

- 缓存的是**原始候选池**，筛选在每次返回时做，所以切换策略或条数不会重新请求维基
- AI 结果按 `月-日 + 条数` 单独缓存 24 小时，使**每天仅 1 次模型调用**
  （前端每 30 分钟轮询，不缓存的话一天会调几十次）
- **防幻觉**：模型返回的年份必须存在于候选池中，否则整条剔除；
  每条保留 `raw_text` 原文便于核对改写是否偏离事实
- 已知局限：prompt 中已禁止添加原文没有的信息，但模型仍可能补充背景评价
  （实测把「展示首台程控计算机」补成「为现代计算机发展奠定基础」）。
  该补充通常正确，但不保证，故保留原文字段
- 内网 API 在阶段二迁到 NAS 后不可达，回退 `curated` 是常态路径而非异常

### 响应式断点

| 断点 | 屏幕宽度 | 照片网格列数 |
|------|---------|-------------|
| xs | < 576px | 1 列 |
| sm | 576-767px | 2 列 |
| md | 768-991px | 3 列 |
| lg | 992-1199px | 4 列 |
| xl | ≥ 1200px | 5 列 |

## 安全考虑

### 已修复的问题

| 位置 | 问题 | 修复方式 |
|------|------|----------|
| `/api/photo/thumbnail`、`/api/photo/full` | 只判断文件 `exists()` 就返回内容，`?path=/etc/passwd` 可读任意文件 | 新增 `_resolve_photo_path()`：解析后必须位于 `IMAGE_DIR` 之下，否则 403 |
| `/api/photos?filter=` | 参数字符串拼接进 SQL，存在注入 | 改参数化查询 `WHERE type LIKE ?`，`LIMIT/OFFSET` 也参数化 |
| `/files/` | 目录浏览暴露文件系统结构 | 受 `ENABLE_FILE_BROWSER` 控制，默认关闭返回 404 |
| `_safe_join()` | 用 `str.startswith` 判断父目录，`/data/photos_evil` 会被误判为 `/data/photos` 的子路径 | 改用 `Path.is_relative_to()` |

> 实现注意：路径校验必须放在 `try` 块**之外**。Flask 的 `abort(403)` 抛的是
> `HTTPException`，它也是 `Exception` 子类，若放在 `try: ... except Exception` 内会被吞掉，
> 403 会变成 200 + JSON 错误体。

### 仍然存在的风险

- **公开照片 WebUI 仍不要求管理员登录**。阶段 2 的认证边界仅覆盖 `/admin/*` 和
  `/api/admin/*`；`FLASK_HOST=0.0.0.0` 时同网段设备仍能浏览公开照片、文案和 GPS 信息。
  仅在可信局域网使用，网络环境不可信时应限制监听地址或在反向代理增加访问控制
- `DOWNLOAD_KEY` 只是路径口令，不是加密。它能拦住随机扫描，拦不住抓过包的人
- 公网部署必须加 HTTPS + 鉴权，或只允许内网访问
- WebUI 可通过 `ENABLE_REVIEW_WEBUI=False` 整体关闭，只留 ESP32 下载接口

