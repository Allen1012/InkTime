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

- `GET/POST /admin/login` 显示或提交登录表单，失败统一提示，不区分账号不存在、密码错误、停用或限流；已限流请求在查询管理员和计算密码哈希前直接返回，未限流且用户名不存在时仍执行 dummy hash；管理员表为空时，登录页显示首次设置入口；
- `GET/POST /admin/setup` 仅在 `admin_users` 表为空时开放；POST 必须通过跨站请求伪造校验和密码二次确认。未配置 `INITIAL_SETUP_TOKEN` 或令牌文件时，页面隐藏令牌字段并提示可信局域网内首位完成设置的人将成为管理员；配置后页面显示必填令牌字段并严格校验，错误令牌返回 HTTP 403。两种模式都调用 `AuthenticationService.create_first_admin()` 原子创建首个管理员；任意管理员存在后 GET 与 POST 均返回 HTTP 404；
- `POST /admin/logout` 仅接受带跨站请求伪造 token 的表单，不提供 GET 退出；
- 除登录和首次设置外，`GET /admin` 和未来新增的 `/admin/*` 页面由 Blueprint `before_request` 默认要求认证；
- `GET /api/admin` 和未来新增的 `/api/admin/*` 接口由另一个 Blueprint `before_request` 默认要求认证；
- 匿名页面请求重定向到 `/admin/login?next=...`，`next` 仅接受以单个 `/` 开头、无 scheme/netloc 的站内相对路径；
- 匿名后台接口统一返回 HTTP 401 JSON；登录后 `/api/admin` 仅返回 `phase=2`、`authentication=implemented` 和当前用户名；
- 公开 Blueprint 整体精确豁免跨站请求伪造校验，兼容现有 `POST /api/render`、`POST /api/settings`；后台登录、退出和未来写接口均校验 token。

后台页面使用 `templates/admin/base.html`、`login.html`、`setup.html`、`index.html` 与本地
`static/css/admin.css`，不依赖外部内容分发网络。认证实现集中在 `auth.py`、`forms.py` 和
`repositories/admin_user_repository.py`：首次设置页面不回显密码或令牌，创建成功后只跳转登录页，不自动建立管理员会话。

### 照片目录扫描入口

直接拷入 `IMAGE_DIR` 的照片不会自动入库，需要扫描才会登记。照片管理页标题行提供「扫描照片目录」按钮，对应 `POST /admin/photos/scan`（页面）与 `POST /api/admin/photos/scan-library`（JSON，返回 202）。

`LibraryScanService` 只做发现、登记与排队：递归遍历照片目录，把未入库的图片以 `analysis_status='pending'` 写入 `photo_scores`，并为每张创建 `analyze_photo` 任务，元数据提取与评分由工作进程完成。

| 规则 | 说明 |
|---|---|
| 支持格式 | `.jpg` `.jpeg` `.png` `.bmp` `.webp` |
| 跳过 | 每个照片目录各自的 `.trash` 回收站目录、路径含 `screenshot` 的截图、非图片文件 |
| 去重 | 按 `path` 判断，且不限定 `is_deleted`——已软删除记录仍占用路径唯一约束，若不排除会导致插入冲突 |
| 单次上限 | 500 张，超出时返回 `remaining` 并提示再次扫描，避免单个事务过大 |
| 任务 payload | 沿用 `{"is_new_upload": false}`，与重新分析一致，确保完整提取 EXIF、GPS 与城市 |

设计取舍：扫描没有引入新的维护任务类型。`admin_maintenance_jobs.job_type` 带 CHECK 约束，SQLite 无法直接修改 CHECK，只能重建表，而迁移框架要求每个文件仅含一条 SQL 语句，代价是六个迁移文件加重建生产数据表。改为复用已在约束内的 `analyze_photo` 类型：遍历目录与写库很快，可在请求内同步完成，耗时的分析仍在工作进程。

命令行与定时任务入口是 `python -m src.analysis.run_scan`，详见 [08-配置与部署](08-配置与部署.md)。

### 回收站页展示

`/admin/trash` 与后台任务页保持同一套表格规范：`.table-wrap` 直接包裹表格，不再嵌套 `.admin-card`，表头与单元格由 `.table-centered` 统一居中。

| 元素 | 说明 |
|---|---|
| 原始位置列 | 唯一左对齐的列（`.trash-path`），路径按字符换行，避免撑宽表格；居中会让长路径难以阅读 |
| 恢复 | 浅绿色图标按钮（`.icon-button.is-success`），POST 到 `/admin/trash/<id>/restore`，携带 `expected_version` |
| 永久删除 | 红色图标按钮（`.icon-button.is-danger`），是链接而非直接提交，先进入确认页 |
| 过期清理 | 橙色按钮（`.button-warning`），位于表格上方的 `.table-toolbar` 内 |

约定：

- 两个行内操作用 `.row-actions` 横向排列并居中，不允许上下堆叠。图标按钮必须同时提供 `title` 与 `aria-label`，否则失去可见文字后无法辨认操作。
- 永久删除必须保留确认页。图标按钮比文字按钮更容易误触，而永久删除不可恢复。
- 页面标题独占一行，记录数与保留期说明移入 `.table-toolbar`：左侧承载上下文，右侧承载操作，两者底部对齐。信息与操作同处一行才能看出该操作作用于下方表格，否则各占一行会显得割裂。
- `.table-toolbar` 距表格 10 像素、距标题区 24 像素。近的一侧决定视觉归属，因此该间距差不可颠倒，否则按钮看起来像页面级操作而非作用于下方表格。
- 工具栏本身始终渲染，只有过期清理按钮在有记录时才出现：回收站为空同样需要显示「共 0 条记录」这类上下文。
- `.page-heading h1:only-child` 会收掉标题自身的下间距。标题独占一行时，`h1` 的 12 像素下间距会与区块的 24 像素叠加，显得过空。

### 照片管理页交互

页面顶部与回收站页同构：标题独占一行，`.table-toolbar` 内左侧是记录数说明、右侧是扫描按钮与视图切换。

**视图切换**使用分段控件而非两个独立按钮。两个视图是互斥选择而非并列操作，因此用 `.segmented` 容器把两片连成一体，容器带 `role="group"`，当前项用 `aria-current="page"` 标记——两个视图对应不同 URL，属于导航，`page` 比 `true` 语义更准确。同一时刻只允许一个元素带 `aria-current`。

**筛选区**由七个控件加一个按钮组构成，共八个栅格列。`筛选` 与 `重置` 必须包在 `.filter-actions` 内占同一个栅格单元，否则窄屏下会被拆到不同行。

**分析状态一律显示中文**，映射与 `forms.py` 中 `PhotoEditForm.analysis_status` 的选项保持一致：历史记录、等待分析、分析中、分析成功、分析失败。筛选下拉、网格卡片、表格列三处共用模板内的同一份字典，修改时必须同步 `forms.py`，否则同一状态在编辑页与列表页会显示成两种说法。

**批量操作栏**采用上下文操作栏模式：未勾选任何照片时不渲染占位，勾选后出现并显示已选数量与清除选择按钮。两条约束不可违反：

- 操作栏在模板中不带 `hidden`，由 `<html class="js">` 配合样式收起。禁用脚本时它常显，批量改分类仍可用；此时状态下拉保持 `disabled`，改状态功能降级但不会出错。
- 取值控件有两个且同名 `value`（分类文本框与状态下拉），未启用的那个必须 `disabled`。`disabled` 控件不参与提交，否则同名字段会一起提交、服务端取到错误值。

**表头全选框不能带 `name`**，否则它自身会作为表单字段提交、污染批量请求。部分选中时用 `indeterminate` 表达，避免看起来像已全选。全选与单项勾选都会同步批量操作栏的计数。

**两种视图的对齐规范**：

| 位置 | 规则 |
|---|---|
| 表格 | 复用 `.table-centered` 居中，照片文件名列 `.photo-path` 单独左对齐 |
| 卡片元数据 | `.compact-meta dt` 统一 `align-self: center` 加 `text-align: center`，让标签与多行取值垂直居中、在标签列内水平居中 |
| 行内操作 | 两种视图共用 `.row-actions` 与 `.icon-button.is-danger`，图标一致 |

**评分展示**为「标签 + 条形图 + 数字」，条宽等于分数，数字向下取整。配色阈值取 `config.DISPLAY_MIN_SCORE`：低于门槛用灰色，门槛以上蓝色，门槛加 15 分以上绿色。用灰色标出低于门槛的照片是有意设计——这些照片永远不会被选片算法选中，纯数字看不出这层含义。未评分显示占位符而不画 0% 的条。

### 照片卡片操作区

`/admin/photos` 网格视图的卡片顶部是一行操作区：批量选择复选框、文件名、右对齐的移入回收站图标按钮。缩略图下方只保留文案与元数据。

| 元素 | 说明 |
|---|---|
| 复选框 | `name="selected"`，`value` 为 `照片编号:版本号`，供批量改分类、改状态与批量移入回收站使用 |
| 文件名 | 即 `title` 字段，由照片路径取末段得到，链接到照片详情；过长时省略号截断并用 `title` 属性展示全名 |
| 图标按钮 | 红色垃圾桶图标，`formaction` 指向 `/admin/photos/<id>/trash`，并以 `expected_version` 携带乐观锁版本 |

约定：

- 复选框没有可见文字，必须保留 `aria-label`，否则屏幕阅读器只会读到一个无名复选框；图标按钮同样需要 `title` 与 `aria-label`。
- 卡片不再显示版本号。版本号属于乐观锁实现细节，对使用者没有意义，但仍随复选框 `value` 与按钮 `expected_version` 提交，并发保护不受影响。
- 卡片内不重复展示文件名。`title` 字段就是文件名，顶部展示后无需再放标题行。
- 表格视图仍使用文字版「移入回收站」按钮，两种视图的提交参数一致。

### 后台侧边栏折叠

侧边栏可在完整宽度与图标窄条之间切换，控件是侧边栏顶部第一个元素——一个与菜单项同款的「收起 / 展开」按钮。

| 项 | 实现 |
|---|---|
| 展开宽度 | 220 像素，图标加文字 |
| 收起宽度 | 72 像素，仅图标居中，指针悬停时显示原生 tooltip |
| 状态存储 | `localStorage` 键 `inktime-admin-sidebar`，值为 `collapsed` 或 `expanded` |
| 首屏恢复 | `admin/base.html` 头部内联脚本，在渲染前给 `<html>` 添加 `sidebar-collapsed` 类 |
| 切换逻辑 | `static/js/admin.js` |
| 图标 | Lucide v1.31.0（ISC 许可）内联 SVG，无外部依赖 |

实现要点：

- 后台是多页应用，每次跳转都重建文档对象模型，因此折叠状态必须持久化；恢复动作放在头部内联脚本而非 `admin.js`，否则会先渲染展开态再收起，产生可见闪烁。
- 折叠按钮与七个菜单项共用 `.nav-item`、`.nav-icon`、`.nav-label` 三个类，按钮只额外重置 `<button>` 的默认外观，样式调整只需改一处。
- 收起时菜单文字用 `clip-path: inset(50%)` 做视觉隐藏，不用 `display: none`，以保留链接与按钮的可访问名称；同时由脚本补上 `title` 属性提供悬停提示。侧边栏设有 `overflow: hidden`，所以不能用伪元素实现 tooltip。
- 按钮在模板中带 `hidden`，由 `admin.js` 移除；禁用 JavaScript 时不会出现失效控件，侧边栏保持展开。
- 宽度过渡为 0.22 秒，`prefers-reduced-motion: reduce` 下关闭。
- 移动端（不超过 800 像素）侧边栏是横向滚动条，收起后只保留一排图标。

### 后台任务列表展示

`/admin/jobs` 合并展示照片任务队列与维护任务队列。两张表的自增主键各自独立，编号仅在同一队列内唯一，因此界面按「中文队列名 #编号」呈现。

| 列 | 展示方式 |
|---|---|
| 编号 | 中文队列名加编号，如「维护 #1」；原始标识 `队列:编号` 保留在悬停提示中，便于对照日志与数据库 |
| 类型 | 中文名称，原始 `job_type` 保留在悬停提示中 |
| 状态 | 彩色徽章 `.status-badge`，等待灰、执行蓝、成功绿、失败红、取消灰 |
| 进度 | 进度条加百分比，条宽由 `progress` 字段（0 至 100）驱动，失败与取消使用区分色 |
| 结果 | 独立成列的中文摘要，例如「生成 42 个展示产物」；无法识别时回退为可折叠的原始 JSON |
| 错误 | 只放错误码与摘要，不再与结果混在同一格；限宽并允许换行 |
| 操作 | 取消与重试按钮始终展示，当前状态不允许的操作置灰禁用并通过 `title` 说明原因 |

标签与摘要在服务层生成（`MaintenanceJobService._decorate` 与 `_summarize_job_result`），模板只负责展示：

- 两个队列的主键各自独立自增，因此编号必须带队列前缀才能唯一定位；`photo` 队列对应照片分析、重写旁白、摘要回填，`maintenance` 队列对应展示产物渲染、回收站过期清理。
- 结果摘要当前识别 `artifact_count`、`counts` 与 `remaining`。未知键名原样保留而非丢弃，计数为零的项不展示以免噪音，非法 JSON 或非字典结构则摘要为空并由界面回退展示原文，确保任何情况下信息都不丢失。

约定：

- 表格使用 `.table-wrap` 加 `.photo-table .job-table .table-centered`，不再额外包 `.admin-card`，避免出现双层边框；照片管理页与回收站页是同一模式。
- 表头与单元格的居中、列间竖向分隔线由通用类 `.table-centered` 提供，任务页与回收站页共用，避免两处样式各自漂移；`.job-table` 只承载任务页专属规则（编号等宽数字、错误列限宽换行）。进度条与操作按钮所在的弹性容器同步居中。
- 操作可用范围由状态决定：等待中或执行中可取消；失败或已取消且 `attempts` 小于 `max_attempts` 可重试。成功的任务两个操作都不可用，此时按钮置灰而非隐藏，以免看起来像功能缺失。
- 进度条带 `role="progressbar"` 与 `aria-valuenow`，百分比数字使用等宽字形避免多行跳动。

### 上传页交互

`/admin/photos/upload` 使用异步提交，页面不再跳转到接口的 JSON 响应。

| 环节 | 实现 |
|---|---|
| 限制来源 | 页面由服务端下发 `UPLOAD_MAX_FILES` 与 `UPLOAD_MAX_BYTES`，写在 `data-max-files` 与 `data-max-bytes`，前端不硬编码 |
| 选择文件 | 拖拽是渐进增强；原生 `input` 用 `sr-only` 保留可聚焦，点击标签与键盘操作始终可用，拖入时靠 `:focus-within` 与 `.is-dragover` 给出反馈 |
| 已选列表 | 每张照片一张卡片，含 `URL.createObjectURL` 生成的缩略图、文件名、大小与移除按钮；重新渲染时 `revokeObjectURL` 释放 |
| 数据源 | `input.files` 是唯一数据源，移除文件时用 `DataTransfer` 重建，保证原生 `required` 校验与提交内容一致 |
| 进度 | 每张照片一条进度条，见下方说明 |
| 结果 | 服务端返回与输入同序的 `items`，逐项标注已接收、重复跳过或失败，卡片边框同步变色 |

必须遵守的两条约束：

- **请求体必须显式构建**。不能用 `new FormData(form)`：上传期间会禁用文件输入以防选择被改动，而 `FormData` 会跳过 `disabled` 控件，导致一个文件都提交不上去。此时客户端校验读 `input.files` 仍然通过（`disabled` 不影响该属性），请求照常发出，最终由服务端返回「至少上传一张图片」，现象极易误判为服务端问题。
- **请求头必须带 `Accept: application/json`**。上传接口在 `accept_html` 且非 JSON 请求时会走 flash 加重定向分支，异步提交将拿不到逐项结果。

关于逐张进度：一次请求内 `XMLHttpRequest.upload` 只提供整批的 `loaded` 与 `total`，浏览器不给单文件粒度。由于 multipart 请求体按文件顺序推送，把已传字节按顺序分摊即可还原每张照片的真实完成度；`total` 含 multipart 边界与字段开销，需先按 `sum(文件大小) / total` 折算回纯文件字节再分摊，否则百分比会整体偏低。若要完全独立的单文件进度，只能拆成每张一个请求，代价是失去服务端「整批校验，任一文件非法则整批不落库、不落盘」的保证。

### 顶部用户菜单

顶部右上角不再放独立的退出登录按钮，改为点击用户名展开下拉面板，退出登录作为面板内的操作项。

实现遵循 W3C ARIA 编写实践的 disclosure 模式，而非 menu button 或 menubar 模式——后两者要求实现完整的方向键导航，本项目的单项菜单不需要这层复杂度。

| 组成 | 说明 |
|---|---|
| 触发器 | 真实 `<button>`，内含首字母圆形头像、用户名与下拉箭头，`aria-expanded` 随状态同步，`aria-controls` 指向面板 |
| 面板 | `.account-menu`，绝对定位于触发器下方右对齐，含当前登录用户说明与退出登录项 |
| 退出 | 仍是携带 CSRF token 的 POST 表单，不退化为链接 |
| 关闭路径 | 再次点击触发器、按 Escape（并把焦点归还按钮）、焦点移出菜单区域、点击页面其他位置 |

约定：

- 面板在模板中不带 `hidden` 属性，而由 `<html class="js">` 配合样式收起。禁用 JavaScript 时面板常驻展开，退出登录依然可用；`js` 类由头部内联脚本添加，因此不会出现先展开再收起的闪烁。
- 退出登录必须保持 POST 加 CSRF 校验。若改为 `<a>` 链接，会被浏览器预取或第三方站点触发，形成登出型跨站请求伪造。
- 箭头旋转与面板淡入动画在 `prefers-reduced-motion: reduce` 下关闭。

### 模板自动重载

`TEMPLATES_AUTO_RELOAD` 由 `APP_ENV` 派生：开发环境为真，生产环境为假。生产使用 Waitress 且非调试模式，Jinja 会缓存模板，修改模板后必须重启服务才生效；开发环境下改完刷新浏览器即可。静态文件不受此影响，始终直接读取磁盘。

### 公开前端动态内容安全

公开相册首页、分类页和照片详情页把接口返回的照片路径、旁白、分类、位置、相机信息及可交换图像文件格式元数据统一视为不可信数据。`main.js`、`category.js` 和 `photo.js` 使用 `createElement`、`textContent`、`dataset` 与显式属性赋值构建页面，禁止把这些字段拼接到 `innerHTML`。照片编号必须先规范化为正整数，评分必须转换为有限数并夹在 0 至 100，缩略图和原图地址只接受同源的 `/api/photo/thumbnail` 与 `/api/photo/full` 路径；非法地址回退到占位图。提示消息同样使用文本节点和独立关闭按钮，避免未来把接口错误文案接入时重新形成持久化跨站脚本入口。

固定分页模板不承载接口字段，可以继续使用静态结构；新增或修改动态页面时，不得用“通用 HTML 转义后继续拼接字符串”代替文档对象模型构建，因为文本、URL、样式和属性属于不同安全上下文。

### 阶段 3 只读照片后台

阶段 3 在现有认证边界内增加以下页面，全部使用服务端 Jinja 渲染，不新增后台写接口：

| 路由 | 页面能力 |
|------|----------|
| `GET /admin` | 三组统计卡片：待处理事项（分析失败、任务失败、进行中任务、回收站、缺拍摄时间，均为可点击入口；失败项非零时标红，缺拍摄时间为引导性提示不标红）、照片概况（总数、近 7 天新增、平均评分、分类数）、分析进度（各分析状态分布、拍摄时间与城市覆盖）；每个统计独立捕获 SQLite 异常并降级，单项失败只显示该卡「暂不可用」 |
| `GET /admin/photos` | 服务端分页、网格/表格切换、缩略图懒加载、搜索、分类、拍摄日期和排序 |
| `GET /admin/photos/<id>` | 照片、文案、评分、相机与可交换图像文件格式元数据只读详情；原文件缺失时仍展示数据库记录 |
| `GET /admin/jobs` | 后台任务列表；阶段 5 起已接入照片任务，阶段 6 起合并展示维护任务 |

公开相册 WebUI、`GET /api/photos` 等公开接口无需管理员登录；`/admin/login` 与管理员表为空时的 `/admin/setup` 允许匿名访问，但两者的 POST 都必须通过跨站请求伪造保护；任意管理员存在后 `/admin/setup` 的 GET 与 POST 都返回 HTTP 404。其余 `/admin/*` 和全部 `/api/admin/*` 统一要求管理员登录，后台写请求还必须通过跨站请求伪造保护。公开与管理员边界按 Blueprint 划分，文档必须分别说明两类访问控制。

后台照片查询由独立 `AdminPhotoService` 编排，继续复用 `PhotoRepository` 和
`MediaService`；公开 `PhotoService` 的字段与分页契约不变。后台列表默认每页 24 条，最大
100 条，排序表达式只接受服务端白名单；搜索覆盖照片路径、描述、旁白与城市，并转义
`%`、`_` 等 SQL `LIKE` 通配符。后台列表支持 legacy、pending、running、succeeded、failed
五种分析状态精确筛选，非法值返回 HTTP 400；不选择状态时仍返回全部 `is_deleted=0` 活动记录。
后台分类选项和首页分类统计始终按全部活动照片聚合，不随状态筛选收缩；公开分类统计继续只计算
legacy 或 succeeded 照片。后台列表和详情使用认证保护、按照片编号定位的媒体路由，
只允许读取 `is_deleted=0` 的活动记录，但不按分析状态过滤，因此管理员仍可查看并处理
pending、running 或 failed 照片。公开缩略图和原图接口继续只允许 legacy 或 succeeded
照片，管理端放宽不会扩大匿名访问范围；所有文件读取仍要求路径位于某个已配置的照片目录内，且不在该目录自己的
`.trash` 目录。

照片详情页提供两个待确认生成入口：`POST /admin/photos/<id>/reanalyze` 重跑完整分析，`POST /admin/photos/<id>/regenerate-narration` 只重写旁白文案。普通表单提交后重定向并提示生成完成后确认保存；携带 `Accept: application/json` 时返回 HTTP 202 和安全任务视图。页面通过 `GET /api/admin/photos/<id>/draft` 查询当前照片版本下的最新草稿，成功后把白名单字段填入编辑表单，**不会自动写入照片**；管理员检查并点击保存后，才通过既有乐观锁更新正式字段。生成任务活跃时两个生成按钮同时禁用，同类重复请求仍由任务唯一约束去重。既有正式 JSON 接口 `POST /api/admin/photos/<id>/reanalyze` 与 `POST /api/admin/photos/<id>/regenerate-narration` 保持正式任务语义，不改为草稿。

照片详情页按数字资产后台常见的信息层级组织：顶部使用单一「原始资产」卡片，桌面端左侧展示大图预览及其当前旁白、画面描述，右侧集中展示原始文件名、存储文件名、分辨率、文件大小、拍摄时间、拍摄城市和可交换图像文件格式（EXIF）元数据，原始元数据摘要保持折叠；窄屏时元数据在同一卡片内移到照片与当前文案下方。下方「管理与生成结果」同样使用单一卡片和与顶部一致的左宽右窄比例：左侧集中编辑分类、画面描述、旁白、回忆分、美观分和评分理由，并单独提供拍摄时间和城市的原始信息修正入口；右侧展示版本、分析状态与错误、创建和更新时间、日期来源及评分。窄屏时右侧生命周期信息移到生成信息下方。

保存、只重写文案、重新分析和移入回收站四个按钮位于同一操作栏，危险动作靠右；窄屏允许换行并铺满宽度。编辑表单与三个动作表单相互独立，全部保留跨站请求伪造保护。`admin-photo-detail.js` 负责初始脏值比较、提交草稿、轮询、页面恢复和安全回填；保存按钮初始禁用，只有可编辑字段（含两项评分）变化后才启用。

缩略图有两个入口——公开的 `GET /api/photo/thumbnail`（按路径，要求分析成功）与后台的 `GET /admin/photos/<id>/thumbnail`（按编号，允许查看 pending 与 failed 照片）——但**生成实现只有一份** `MediaService.render_thumbnail()`。早期两条路径各自硬编码 300×200，改了配置只有公开接口生效、后台页面看着毫无变化，因此后台入口改为复用同一方法。缩略图长边与质量取自 `THUMBNAIL_MAX_EDGE`（默认 640）与 `THUMBNAIL_QUALITY`（默认 82），可在线调整。原实现固定 300×200，而后台网格单元约 293 px 宽、高清屏需要约 586 px 像素，等于先缩小再放大近三倍，必然模糊。生成时先用 `draft()` 让 libjpeg 直接以缩小比例解码，再用 LANCZOS 缩放：四千像素级原图整幅解码很贵，而翻页时缩略图请求非常密集——实测长边从 300 提到 640（清晰度约 2.9 倍）后单次耗时几乎不变（158 ms → 162 ms）。小图不放大。

缩略图有两级缓存。**服务端磁盘缓存**位于 `data/cache/thumbnails`，按源路径摘要前两位分桶，键名含源文件修改时间与体积、长边与质量，因此换照片、改照片或调配置都会落到新键，无需显式失效；写入时用临时文件加 `os.replace`，并顺带清掉同一张照片的旧变体，避免无限堆积。缓存目录刻意不放在照片目录内——那里的 JPEG 会被扫描当成照片入库。由 `THUMBNAIL_CACHE_ENABLED` 控制，缓存读写失败只记录告警、不影响响应。**浏览器缓存**通过 `Cache-Control: private, max-age` 与弱 `ETag` 实现，支持 `If-None-Match` 返回 304。两条路由都**先算校验值再决定是否生成**：校验值只需 `stat()` 与两个配置值，而生成要解码四千像素级原图；早期实现顺序相反，304 只省带宽不省 CPU。校验值由源文件修改时间、体积与影响输出的两个配置共同构成，因此改尺寸或质量后浏览器会自动重新拉取，不会继续用旧缓存里的模糊图。

后台页面的时间统一经 `readable_time` 过滤器渲染为「2026年1月31日 14:27」：`photo_scores.exif_datetime` 存的是 EXIF 标准格式 `YYYY:MM:DD HH:MM:SS`，照片分析、每日渲染与展示选片都按该格式解析，因此**存储保持原值、只在展示层格式化**。带时区的时间戳（创建、更新、删除时间以协调世界时存储）会换算到本机时区再显示，不带时区的 EXIF 拍摄时间按本地时间直接显示——EXIF 没有时区信息，擅自换算只会让时间变错。无法解析的值原样返回，不隐藏数据也不报错。回收站清理预览的截止时间在展示处格式化，表单隐藏域仍提交原值。

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

公开媒体端点不再仅判断路径位于单一照片目录：解析后先确定所属根、拒绝该根的 `.trash`，再回查该路径必须对应
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

这些路由继承管理员 Blueprint 的登录与跨站请求伪造保护。页面和接口只允许修改注册表中 `editable=true` 且 `restart_required=false` 的配置；未知键、类型错误、非法枚举、越界值或只读项会使整批零写入。提交使用全局版本乐观锁，旧版本返回 HTTP 409；配置、版本和审计处于同一事务。全部值未变化时不递增版本，也不产生空审计。

配置页按「日常调什么」分成六个标签，标签布局由 `admin.py` 的 `_SETTINGS_TAB_LAYOUT` 声明，与注册表的 `group` 字段解耦：

| 标签 | 内容 | 附带的只读信息块 |
|------|------|----------------|
| 模型与分析 | 模型接口（接口地址、模型名、密钥、超时、图片最长边同段）、照片目录、地点与城市推断 | 照片目录状态（挂在「照片目录」段上方）|
| 展示与天气 | 站点与功能开关、展示页与轮播、生效时间段与休息期、缩略图、天气、历史上的今天 | 展示生效时间段解析结果（挂在「生效时间段与休息期」段上方）|
| 渲染与设备 | 每日选片、渲染字体 | 设备下载地址（面板级）|
| 上传与任务 | 上传限额与压缩、后台任务、回收站 | 无 |
| 系统与安全 | 运行环境与路径、密钥、会话与登录限流（全部只读） | 无 |
| 配置审计 | 无配置项 | 最近配置审计（表格直接落在面板里，不套信息块）|

只读块分两级：与某一段配置强相关的写在该段的 `card` 字段上，紧贴那一段上方渲染；与整个标签相关、不属于任何一段的仍留在标签的 `cards` 里、渲染在面板顶部。这样「照片目录状态」就挨着 `IMAGE_DIR`、「展示生效时间段解析结果」就挨着时间段配置——它们本来就是同一件事的两半：先看清现状，再改配置。块自己上方已经有一条分隔线，因此紧随其后的分段用 `.settings-info-block + .settings-section` 去掉自己的上边线，避免两条线贴在一起。

模型接口地址、模型名与密钥必须同段：接入一个模型本来就是一次操作，散在「分析」和「安全」两个分类里会让人来回跳。只读的系统与安全项集中到最后一个带配置项的标签，不占据日常位置。「配置审计」是不含任何配置项的纯记录标签，排在最末：审计表又宽又长，塞在系统与安全项下面会把那一屏撑乱。它的 `sections` 为空，因此标签上不渲染项数角标（避免显示「0」），未登记配置项的「未分类」兜底段也会跳过它、落到最后一个带配置项的标签。

`_settings_tabs()` 按布局重排配置项并统计每标签的项数、可编辑数与校验错误数。布局里没登记的配置项不会被丢弃，而是兜底进入最后一个标签的「未分类」段，因此新增配置项即使忘记登记也能显示和提交；`SettingsTabLayoutTestCase` 断言注册表与已分类集合完全相等，漏登记会直接测试失败。

全部配置项渲染在同一个 `<form>` 内，标签切换只做面板显隐，所以保存一次提交所有可编辑项，与当前停留在哪个标签无关。保存栏用 `position: sticky; bottom: 0` 吸底，不必滚到页面末尾；sticky 的 `bottom` 只能把元素向上偏移，因此保存栏必须是表单里最后一个流内元素，在它后面追加内容会让它滚过自然位置后脱离底部。可编辑项渲染为输入框、下拉框或数字框，锁定项渲染为禁用文本框，需重启的项额外显示「需重启」标记。可编辑的敏感项（当前只有 `API_KEY`）渲染为空值密码框，留空提交表示保持原值，填值则覆盖；页面、接口与审计都不回显真实密钥。`PROJECT_NAME` 由公开页面在每次渲染时热读取，改完刷新即生效。

每个配置项只常显两部分：标签行的中文名（加「需重启」标记）和控件；校验错误照旧常显在控件下方。**说明、英文键名与来源都收进悬停层** `.settings-field-tip`，层内顺序是键名在上、说明在下：鼠标悬停或键盘聚焦名称（`:hover` 与 `:focus-within`）即展开。名称只用 `cursor: help` 暗示还有信息，不画下划线——70 项名称全带装饰线反而更乱。悬停层紧贴名称下沿（`top: 100%` 不留缝隙），否则鼠标移向层内按钮时会先离开触发区导致闪烁。展开与否是纯 CSS，无脚本环境同样能看到说明和键名。

键名不能删：14 项只读配置只能在部署环境按键名改，校验失败时页面顶部错误列表与 `/api/admin/settings` 都以键名为标识，README 与部署文档也只用键名称呼配置项；搜索框的 `data-search-text` 仍含名称、键名与说明，所以移进悬停层不影响搜索。键名渲染成 `<button class="settings-field-key" data-copy-text="KEY">`，点击即复制。复制走 `admin-settings.js` 的事件委托（70 项逐个绑监听器没必要），优先 `navigator.clipboard`，失败或不可用时退回临时 `textarea` 加 `document.execCommand('copy')`，彻底失败则用 Range 选中键名让用户自己按 Ctrl+C——**家庭局域网通常是普通 HTTP，非安全上下文里 `navigator.clipboard` 根本不存在**，只用它会让复制在最常见的部署形态下静默失效。按钮用 `data-copied` 属性显示「已复制」，同时写一条 `.sr-only` 活动区域播报给读屏软件。键名按钮位于 `<label>` 内部，处理器调用 `preventDefault()` 以免顺带聚焦并滚动到对应控件。

**`来源：` 不再逐项复述。** 旧版把 `source` 直接印在每一行，但 Web 进程通过 `_configuration_initial_values()` 把 Flask 配置默认值一起当作启动值传给配置服务，于是几乎每项都显示 `environment`，既不代表部署方设过这一项，也不提供任何决策依据。现在 `ConfigurationService` 额外接收 `environment_keys`（Web 进程只传真正出现在 `os.environ` 里的键），管理视图据此给出两个布尔值：`from_environment` 表示该项确由部署环境设置，`environment_overridden` 表示这个环境值已被数据库覆盖压住。悬停层只在三种反直觉情形显示说明：

| 情形 | 显示 |
|------|------|
| 只读或需重启项 | 只能在部署环境修改，并说明当前值是来自环境变量还是启动默认值 |
| 可编辑项且 `environment_overridden` | 黄字提示「已被在线配置覆盖，同名环境变量不再生效」——这正是「改了 `.env` 重启却没生效」的成因 |
| 可编辑项且 `from_environment` | 「值由部署环境设置，保存后由数据库接管」 |

其余情况悬停层里只有说明和键名。谁在什么时候改了什么，去「配置审计」标签查。

`admin-settings.js` 负责标签交互：点击或左右方向键切换，`localStorage` 的 `inktime.settings.activeTab` 记住上次所在标签，URL hash 可直接定位。标签本身是紧凑长方形（`border-radius: 6px`、最小高度 32 像素、16 像素图标），不用胶囊形：六个标签横排还要给同一行的搜索框留位置，圆角胶囊加大内边距会把横向空间吃掉。窄屏（≤860px）下标签恢复到 38 像素高，保证触屏点击区域。校验失败时服务端在出错标签上渲染红色错误计数，脚本优先切到该标签，避免用户在别的分类里找不到出错项。搜索框跨全部标签过滤配置项名称、键名与说明，命中时平铺展示所有分类的匹配项，按 Esc 或清空返回分类浏览。无脚本环境下标签栏与搜索框隐藏、所有面板强制展开（`html:not(.js)`），配置仍可查看和提交；悬停层由纯 CSS 控制，此时说明与键名照样能看，只是不能一键复制。

「设备下载地址」块位于渲染与设备标签内，属于面板级（与任何一段配置都不强相关）。服务端用当前请求的协议和主机构造 `GET /static/inktime/{key}/latest.bin` 完整绝对地址，只向已认证管理员展示；页面提供只读文本框和复制按钮，剪贴板接口失败时允许手工选择。该地址包含 `DOWNLOAD_KEY` 路径口令，不应截图、公开分享或写入日志；反向代理部署还必须正确传递外部协议和主机。

「照片目录状态」表位于模型与分析标签内、紧贴「照片目录」段上方，逐行展示每个已配置目录的路径、是主目录还是附加目录、可用性（可读写、只读、不可读、目录不存在）以及该目录下的活动照片数与回收站照片数。数据来自 `PhotoLifecycleService.image_directory_status()`：只做只读探测与按目录前缀计数，某个目录消失时标为「目录不存在」而不是让页面报错。后台可在线修改 `IMAGE_DIR`，但保存只接受容器已经挂载、存在且可读的目录；修改配置不会创建 Docker 挂载，外部照片库必须先通过 Compose override 挂载到所有消费者。

「展示生效时间段」块位于展示与天气标签内、紧贴「生效时间段与休息期」段上方：展示 `DISPLAY_ACTIVE_WINDOWS` **解析后**的人类可读摘要（`describe_time_windows()`，相同安排的星期合并为「周一至周五」，无区间的星期单独列为「全天休息」）、按当前切换模式估算的每天切换次数（`estimate_daily_rotations()`，仅 `hourly` 与 `interval` 给出），以及一组常用预设按钮。摘要与估算解决两个实际踩坑：区间左闭右开导致 `09:00-22:00` 在整点模式下最后一次是 21:00 而非 22:00；只配工作日会让画面在周末连续静止约 58 小时。配置无法解析时卡片显示错误原因并说明展示页按全天生效运行。

预设按钮由 `admin-settings.js` 处理，只把预设值填进对应输入框（通用渲染已为每个可编辑控件生成 `id="setting-<KEY>"`），不做任何前端解析：解析规则只保留服务端一份，避免跨零点归属与区间合并这类边界两处漂移。

「最近配置审计」占据独立的「配置审计」标签（`#settings-panel-audit`），面板里只有一张审计表：时间、版本变化、修改人与逐项新旧值。表格直接落在面板里（`.settings-audit-table` 只给上边距），不再包一层信息块，全屏只有表格自身一层边框。该面板没有可命中的配置项，搜索时脚本会整块隐藏它，不会留下空面板。

三处仍需标题与说明的只读信息块（照片目录状态、设备下载地址、展示生效时间段）共用 `.settings-info-block`，它与 `.settings-section` 同构：只有标题、细分隔线和内容，没有边框和底色。面板本身已经是一张卡片，信息块再带边框就会在面板里嵌套第二层框，叠上表格自己的 `.table-wrap` 边框会出现三层圆角框。`AdminPagesRenderTestCase` 的 `test_settings_audit_has_its_own_tab` 与 `test_settings_info_blocks_are_flat_inside_panels` 分别断言审计面板不含配置控件与信息块、以及每个面板最多一块信息块。

上传与浏览相关配置同样不再冻结在服务实例上：`UploadService` 在每次上传开始时取一份上限快照，`FileBrowserService` 在每次浏览时读取「产物目录浏览总开关」（`ENABLE_REVIEW_WEBUI`）与「启用产物目录浏览」（`ENABLE_FILE_BROWSER`），两者串联、同时为真才开放 `/files/`，都不影响照片墙、分类、搜索与展示页；回收站页面的默认保留天数取自 `PhotoLifecycleService.retention_days` 的动态读取。应用另有 `before_request` 钩子按当前上传上限重算 Flask 的 `MAX_CONTENT_LENGTH`，否则上限改大后请求仍会被启动时算出的旧值拦截。

敏感配置只显示是否已配置，真实值不进入页面、接口、审计或任务快照。公开 `GET /api/settings` 保持原裸 JSON 字段，并按请求读取展示轮换配置；公开 `POST /api/settings` 仍是无需认证的兼容模拟响应，不会修改真实配置。

请求期热更新覆盖 `DISPLAY_TEMPLATE`、`DISPLAY_ROTATE_MODE`、`DISPLAY_ROTATE_INTERVAL_SEC`、`DISPLAY_KEEP_AWAKE`、`DISPLAY_UI_HIDE_DELAY_SEC`、`DISPLAY_MIN_SCORE`、`DISPLAY_NEW_PHOTO_WEIGHT`、`ONTHISDAY_COUNT`、`ONTHISDAY_SOURCE`、`ONTHISDAY_STRATEGY`、`ONTHISDAY_MIN_YEAR` 和 `PANEL_AI_MODEL`。展示选片阈值与权重作为单次调用参数贯穿算法；信息面板条数、数据源、策略、年份和模型也显式贯穿筛选链路，候选池缓存键包含数据源、人工智能结果缓存键包含数据源与实际模型，切换数据源或模型不会继续命中旧结果。

`GET /api/display/next` 在生效时间段之外返回新的 `status=idle` 响应，段内响应保持不变。idle 响应携带 `idle_mode`、`message`、`resume_at`、`next_check_after_sec` 与可选的 `data`（`freeze` 与 `photo` 模式下结构与正常响应一致）。服务端在调用 `gallery.pick_next()` **之前**完成时间段判定，因此段外既不切换照片也不写 `display_stats`；`freeze` 画面由新增的只读 `gallery.peek_photo()` 提供，同样不记账。

`display.js` 识别 `idle` 后进入休息态：有照片则复用 `renderPhoto()`（同一张不重复渲染以免反复触发图片加载），无照片则显示 `.rest-overlay` 遮罩；调度改用 `next_check_after_sec` 退避，收到正常响应即恢复原有对齐节奏，右上角状态标注「休息中」。遮罩使用视口级 `fixed` 定位，因为 `rest` 模式下 `img` 无 `src` 会让 `.photo-container` 塌缩成零尺寸。按产品约定休息期不展示恢复时间。休息期手动点「下一张」仍会正常取片并记账。

`GET /api/panel` 在原有 `date`、`lunar`、`onthisday` 之外新增 `weather` 段，由 `PanelService.get_data()` 在服务层合并，不改动动态加载的 `panel.py` 模块契约。取数实现位于 `src/server/weather.py`：标准库 `urllib.request`、超时 5 秒、带线程锁的 TTL 缓存（缓存键含坐标）、WMO 代码到中文与图标映射、风速换算蒲福风级、风向转八方位中文。任何异常都降级为 `{"available": false}`，并在曾成功过的情况下返回上次数据且标注 `stale`。

仪表盘模板的天气块位于时钟与农历之间，显示图标、温度、状况、体感、湿度与风力，数据不可用时整块隐藏；沉浸式模板只在左上角放图标加温度的极简角标（右上角已被自动播放与常亮状态指示器占用），默认关闭。两者的 DOM 元素都由服务端按 `DISPLAY_WEATHER_SHOW` 与 `DISPLAY_WEATHER_CORNER` 条件渲染，元素不存在时前端不会发起任何天气请求。天气图标是 `templates/_weather_icons.html` 里的内联 SVG 精灵，通过 `<use href="#wi-xxx">` 引用，id 与 `weather.WEATHER_ICONS` 一一对应；不用 Font Awesome 4.7 是因为它缺雾、雨夹雪、阵雨、冰雹等状态图标。仪表盘的天气按 10 分钟单独刷新，与 30 分钟的面板刷新解耦。

分析、渲染和工作进程已接入任务配置快照：独立工作进程在任务首次认领的同一事务中按任务类型固化 `analysis`、`render` 或 `worker` 作用域快照；租约恢复与自动重试继续沿用原快照，人工重试重新固化为当前配置。

## 服务器配置

配置启动初始值来自进程环境，项目 `.env` 只在存在时作为不覆盖环境的可选补充；基础 Docker Compose 不依赖 `.env`。`create_app(config_overrides=None)` 在函数内加载配置，再应用可选覆盖；覆盖主要用于指向临时数据库的验证，不修改环境变量。路径值统一按项目根目录解析后写入 `app.config`。

`APP_ENV` 只允许 `development`、`testing`、`production`。基础 Compose 明确使用 `development`、普通 HTTP 和 `SESSION_COOKIE_SECURE=False`，仅适合可信家庭局域网；systemd Web 服务仍使用生产模式。`SECRET_KEY` 与 `DOWNLOAD_KEY` 未显式配置时，在数据库结构门禁通过后分别从数据库同目录 `.inktime-secret-key` 与 `.inktime-download-key` 读取或原子创建，新文件权限为 `0600`。显式应用覆盖或环境变量优先；已有文件权限向组或其他用户开放、内容含空白或长度不足、不是普通文件或无法安全读取时拒绝启动。生产模式仍要求最终会话密钥非空、下载密钥至少 24 个字符且不为 `inktime`，并要求 `SESSION_COOKIE_SECURE=True` 与 HTTPS 配套。

上传文件数、单文件字节数和像素数统一夹在 1–10、1–20 MiB、1–80,000,000 范围，Flask `MAX_CONTENT_LENGTH` 派生为“文件数 × 单文件字节数 + 1 MiB multipart 元数据预算”。超过上限统一返回 HTTP 413 与“请求体过大”，不回显长度或文件名；`UploadService` 的数量、单文件和像素保护继续保留。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| FLASK_HOST | 0.0.0.0 | 监听地址 |
| FLASK_PORT | 5005 | 监听端口（本机部署实际用 8888） |
| DB_PATH | ./data/photos.db | 数据库路径 |
| IMAGE_DIR | ./data/photos | 相册目录，同时是照片读取接口的安全边界 |
| BIN_OUTPUT_DIR | ./data/output | 渲染产物目录 |
| DOWNLOAD_KEY | 空 | ESP32 下载路径密钥；production 必须至少 24 个字符且不能为 `inktime` |
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
| ONTHISDAY_SOURCE | baidu | 数据源：baidu（百度百科）/ 60s（60s 开源接口）/ wikipedia（维基百科） |
| ONTHISDAY_STRATEGY | curated | 筛选策略：recent（最近优先）/ curated（规则精选）/ ai（模型筛选） |
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

`run_server.py` 先调用应用工厂；应用工厂归一化 `DB_PATH` 后立即执行只读 `assert_current_schema()`。结构门禁通过后，才为缺省的 `SECRET_KEY` 与 `DOWNLOAD_KEY` 读取或创建数据库同目录持久化文件，然后执行安全配置归一化、创建输出目录并注册 Service 与 Blueprint。错误或旧数据库旁不会先写入新密钥状态。`server.py` 只重导出 `create_app` 并保留直接执行兼容入口。生产 Web 服务与独立工作进程启动时都要求迁移版本集合恰好为 1 至 50，且每个版本的名称与 SQL 文件 SHA-256 校验值匹配当前程序；未来版本和分叉历史会直接拒绝启动，且不会自动迁移。结构门禁通过后，两类常驻进程会认领并对账租约已经过期的照片生命周期操作，使文件严格对齐数据库前态或后态；该恢复不替代显式结构迁移。高版本数据库必须先升级程序，不能用旧程序继续写库。部署应先显式运行 `scripts/database_admin.py migrate`，再以只读 `check-schema` 门禁确认目标版本 50。


## API 接口

### ESP32 下载接口

| 端点 | 说明 |
|------|------|
| `GET /static/inktime/{key}/photo_{idx}.bin` | 下载第 idx 张渲染照片 |
| `GET /static/inktime/{key}/latest.bin` | 下载最新渲染照片 |
| `GET /static/inktime/{key}/preview.png` | 下载预览图 |

`{key}` 必须匹配 DOWNLOAD_KEY，否则返回 404。认证管理员可在 `/admin/settings` 查看并复制完整的 `latest.bin` 绝对地址；匿名访问后台设置页不会获得该地址。

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
| 历史上的今天 | `/api/panel`（百度百科 / 60s / 维基百科可切换） | 数据源、条数与筛选策略可配；数据源失败时整块隐藏 |

布局要点：右栏 `justify-content: center` 整体上下居中，因此历史区必须用
`flex: 0 0 auto` 而非 `1 1 auto`，否则它会撑满剩余空间破坏居中效果。

### 「历史上的今天」数据源

`ONTHISDAY_SOURCE` 三选一，界面与接口一律显示中文名（配置值保持英数字标识，
避免中文值写进 `.env`、容器环境变量与数据库）：

| 配置值 | 中文名 | 接口 | 特点 |
|--------|--------|------|------|
| `baidu`（默认） | 百度百科 | `baike.baidu.com/cms/home/eventsOnHistory/{MM}.json` | 国内直连、免密钥；按月返回整月数据（约 400 KB），带 `type` 事件类型；`title` 含词条超链接标签，必须剥成纯文本 |
| `60s` | 60s 开源接口 | `60s.viki.moe/v2/today_in_history?date=MM-DD` | 百度数据的开源二次封装，字段已清洗；多一层他人服务，作备用 |
| `wikipedia` | 维基百科 | `api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/...` | 原默认源；国内网络常不可达，且返回繁体需转简体 |

改默认源的原因是可达性：同一台机器上百度百科 130 毫秒返回，维基百科在
Python `urllib` 下直接 SSL 握手超时（`curl -4` 偶尔能通），无 IPv6 的网络附加
存储设备上尤其明显。

实现要点：

- 取数按数据源分派到 `_fetch_baidu` / `_fetch_sixtys` / `_fetch_wikipedia`，
  三者统一输出 `{year, text, type}`，`text` 经 `_strip_html` 剥标签与实体
- 年份用 `_parse_year` 解析，「前221」这类公元前写法转为负数
- **候选池缓存键包含数据源**，切换数据源不会继续命中上一个源的缓存；
  人工智能筛选缓存键同时包含数据源与实际模型
- 60s 会校验返回的月日与请求一致，不一致视为取数失败
- 非法数据源值回退百度百科并打印告警，不让面板整块不可用
- 返回体同时给出 `source`（标识）与 `source_name`（中文名）

### 「历史上的今天」筛选策略

历史事件接口的选材本身偏重灾难、战争与逝世 —— 实测某日 30 条候选里，
按年份取最近的两条会得到「空难 21 人罹难」和「特大泥石流灾害」。
家庭相框场景不适合天天展示这类内容，因此提供三种策略：

| 策略 | 中文名 | 做法 | 特点 |
|------|--------|------|------|
| `recent` | 最近优先 | 纯按年份降序 | 最简单，但内容常偏沉重 |
| `curated` | 规则精选 | 按 `type` 剔除逝世类 + 过滤负面关键词 + 年份下限 + 周年/长度加分 | 无外部依赖；能挡掉明显灾难，挡不掉枯燥的百科腔 |
| `ai` | 模型筛选 | 大模型挑选并改写 | 效果最好，失败自动回退 `curated` |

百度百科与 60s 带 `type` 字段，逝世类按类型精确剔除，比关键词命中可靠；关键词
表同时补齐了原先漏掉的「逝世 / 去世 / 病逝」，维基源也一并受益。

实现要点：

- 缓存的是**原始候选池**，筛选在每次返回时做，所以切换策略或条数不会重新请求数据源
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

