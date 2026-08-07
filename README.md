# InkTime · 墨水屏回忆相框

<p align="left">
  <img src="esp32/InkTime.jpeg" width="80%">
</p>

InkTime 是一个「拉回你相册里的记忆」的墨水屏电子相框项目。

它不会随机展示照片，也不是简单的按时间轴播放，而是：

- 用 AI 理解每一张照片在"“"拍什么"
- 给照片按照"值得回忆度"、"美观度"打分
- 写一句灵光一现的旁白文案
- 每天从"历史上的今天"里选出**最值得被再次看到的照片**
- 推送到 ESP32 墨水屏上展示

---
## 项目整体结构

InkTime 分为三部分：

1. **照片分析（Python）**  
   扫描相册 → 调用视觉模型 → 分类 / 评分 / 写文案 → 存入数据库


2. **图片渲染（Python）**  
   从数据库里选出「历史上的今天」高分照片 → 渲染成 ESP32 可直接显示的 `.bin`


3. **下载与展示（ESP32）**  
   ESP32 定时从服务器拉取 `.bin` → 刷新墨水屏 → 深度休眠直至下次唤醒

---
## 环境准备

### 1）Python
推荐 Python 3.10+。

建议使用虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2）安装 exiftool （可选）
InkTime 可以在不装 exiftool的情况下运行，但不一定能完整地获取 EXIF 中的 GPS 信息。

建议使用 exiftool 获取 GPS 信息：  

MacOS(Homebrew): ```brew install exiftool```  
Linux: ```sudo apt-get install -y libimage-exiftool-perl```

### 3) 配置 .env
```
cp .env.example .env
vi .env
```
必须配置以下字段：  
照片库路径 ```IMAGE_DIR```  
VLM 模型接口 ```API_URL``` ```MODEL_NAME``` ```API_KEY```  
中文字体路径 ```FONT_PATH```（留空会把中文渲染成豆腐块，且不报错）  
InkTime 使用 OpenAI 接口（LM Studio / 云端兼容服务均可）。

```.env``` 是唯一配置源，各脚本会自行加载它，不需要先 ```source .env```。  
注意值含空格时要加引号，例如 ```PROJECT_NAME="InkTime 相册"```。

为防止照片隐私泄露，建议修改```DOWNLOAD_KEY```，为 ESP32 下载路径加一个随机前缀作为密钥。   
同时，请同步修改```esp32/ink-display-7C-photo/ink-display-7C-photo.ino```固件中的```DAILY_PHOTO_PATH_PREFIX```字段。  
注意，这不是“加密”，只是一个简单的验证路径口令。公网部署建议加 HTTPS/反代鉴权，或只允许内网访问。  
另外 WebUI 本身没有任何登录鉴权，```FLASK_HOST=0.0.0.0``` 时同网段设备都能浏览你的全部照片与 GPS 信息，请只在可信局域网内使用。

## 分析照片
分析照片前，请先确保：
- LM Studio（或你的云端 VLM 服务）已启动
- .env 已正确配置

执行：

```./venv/bin/python src/analysis/analyze_photos_docker.py```

也可以用封装脚本（自动加载 .env 并使用 venv 的 python）：

```./scripts/run_analysis.sh```

建议先小批量试跑，确认文案风格和评分标准符合预期后再全量：

```BATCH_LIMIT=20 ./venv/bin/python src/analysis/analyze_photos_docker.py```

视觉大模型会读取并理解相册目录中的所有文件，为每张照片生成：

- 画面描述
- 照片类型
- 值得回忆度 / 画面美观度评分
- 一句话文案

图片数据会保存在```data/photos.db```中（SQLite数据库），第一次运行会自动建库。

请自行修改```src/analysis/analyze_photos_docker.py```中的提示词，以调整模型的评价标准和文案风格。

程序可以断点续跑，已处理过的照片信息不会重复分析。你可以分几天分析完你的整个相册。

*请根据你拥有的算力选择合适的模型，作者使用的 qwen3-vl-30b 已经能取得相当不错的文案。*  
*注意每张照片会消耗 2 次 API 调用（一次评分描述、一次文案），用云端 API 时按张数×2 估算额度。*

> **重要**：没有 EXIF 拍摄时间的照片（截图、微信保存、导出压缩过的图等）不会进入选片候选池，
> 评分再高也永远不会被展示。分析完可以查一下可用比例：
> ```
> ./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/photos.db').cursor();print(c.execute(\"SELECT COUNT(*) FROM photo_scores WHERE exif_datetime IS NOT NULL AND exif_datetime!=''\").fetchone(), c.execute('SELECT COUNT(*) FROM photo_scores').fetchone())"
> ```

## 为 ESP32 渲染"历史上的今天"照片
执行：

```./venv/bin/python src/render/render_daily_photo.py```

产物在 ```data/output/```：```photo_{idx}.bin```、```latest.bin```、以及可用于肉眼检查效果的 ```preview.png```。

## 启动 ESP32 下载服务器和 WebUI
本地调试：

```./venv/bin/python src/server/server.py```

常驻运行请用 waitress（见下方 systemd 示例），不要用 Flask 开发服务器。

#### WebUI（如果开启）：
Server 将提供一个简明的可视化前端，用于查看已处理照片的描述、文案，并预览模拟墨水屏渲染效果。

在浏览器中访问（端口取 ```.env``` 里的 ```FLASK_PORT```，默认 5005）：

```http://127.0.0.1:5005/```

可用页面：```/```（照片墙）、```/category```（分类）、```/search```（搜索）、```/display```（沉浸式展示）。

程序跑通后，建议在```.env```中把```ENABLE_REVIEW_WEBUI```设为 False，仅保留 ESP32 下载接口。

## 服务器部署与定时任务示例（可选）

先装生产 WSGI 服务器：

```./venv/bin/pip install waitress```

仓库里已有现成的单元文件 ```deploy/inktime-server.service```，按需修改 ```User```、```Group```、路径后：

```
sudo cp deploy/inktime-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inktime-server
```

单元文件内容（host/port 从 ```.env``` 读取，改端口只需改 .env 后 restart）：

```
[Unit]
Description=InkTime Server
After=network.target

[Service]
Type=simple
User=inktime
Group=inktime
# 改成你的项目路径
WorkingDirectory=/path/to/InkTime
EnvironmentFile=/path/to/InkTime/.env
Environment=PYTHONPATH=/path/to/InkTime
ExecStart=/path/to/InkTime/venv/bin/waitress-serve \
    --host=${FLASK_HOST} \
    --port=${FLASK_PORT} \
    --call src.server.server:create_app
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

验证：

```
systemctl is-active inktime-server
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5005/
journalctl -u inktime-server -f --since "10 min ago"
```

使用 crontab 每天凌晨自动选片、渲染：

```
chmod +x scripts/daily_render.sh
sudo -u inktime crontab -e
0 5 * * * /path/to/InkTime/scripts/daily_render.sh
```

```scripts/daily_render.sh``` 的项目路径会按脚本自身位置自动推导，不需要手改。  
在 ```logs/render.log```可查看日志。

---

# ESP32 墨水屏硬件部分

## 硬件与引脚
#### 主控
本项目使用乐鑫 ESP32-S3-N8R8 模块。  
当然，你也可以使用任何成品 ESP32 开发板进行制作。  
如使用其它开发板或模块，请注意选择带 PSRAM 的型号（需至少 384K PSRAM）。  
#### 屏幕
本项目使用 7.3 寸四色墨水屏，型号为 EL073TS3（49-pin）。使用 GxEPD2 库驱动（GxEPD2_730c_GDEY073D46）。  
其它尺寸、型号请自行参照 GxEPD2 库中的硬件支持列表修改构造函数。
#### 墨水屏转接板
本项目使用 B 站"记得带马扎"制作的七色 EPD 墨水屏转接板（49-pin）。  
但市面上的大部分 24-pin 墨水屏搭配 SPI 转接板亦可兼容。

#### 引脚定义
墨水屏使用 SPI 通信，本项目默认引脚为：
- `PIN_EPD_BUSY = 14`
- `PIN_EPD_RST  = 13`
- `PIN_EPD_DC   = 12`
- `PIN_EPD_CS   = 11`
- `PIN_EPD_SCLK = 10`
- `PIN_EPD_DIN  = 9`

### 主板焊接
原理图、BOM清单、制板文件均位于```esp32/pcb```文件夹中。  
原理图中的 H1 - H6 为测试焊盘引出，无需焊接真实器件：
- H1: UART 串口
- H2: USB
- H3: BOOT引脚，烧录固件时需将改引脚短接到 GND 后上电
- H4: 焊接至 EPD 墨水屏转接板
- H5: 3.7V 电池焊盘
- H6: 5V 输入测试焊盘

建议使用 UART 串口烧录固件。R2、R3、C5、C6 供 USB 使用，如无需要，可留空不焊。

SW1：RESET 键，按下后会重启设备，并从服务器拉取、显示图片一次。RESET 键可将设备从长休眠状态中唤醒。  
SW2：WiFi 重置键，按住 SW2 再按下 SW1，ESP32 重启后会清空 NVS，以重新配置 WiFi 连接。  
SW3 / SW4: 备用 GPIO，以防未来需要添加的功能。如无需要，可留空不焊。

完整 PCB 板示例：

<p align="left">
  <img src="esp32/pcb/pcb.jpeg" width="80%">
</p>

## 编译与烧录

建议使用 Arduino IDE。

1. 安装 ESP32 Arduino Core。
2. 选择开发板：ESP32-S3（必须开启 PSRAM）。
3. 安装依赖库：
   - `GxEPD2`
4. 打开并编译/烧录 `ink-display-7C.ino`。

### 自定义字体(可选)
如需使用自定义中文字体，把字体文件路径写进 ```.env``` 的 ```FONT_PATH``` 即可。  
Ubuntu 可直接用系统自带的 ```/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc```。  
该项留空时中文会渲染成豆腐块且不报错。

## 首次配置

设备启动时，会尝试从 NVS 读取已保存的 Wi-Fi 配置；若未配置或 Wi-Fi 连接失败，会自动进入 AP 配置模式：

- 设备会开启 AP 热点：`InkTime-xxxx`
- 默认密码：`12345678`
- 连接 AP ，用浏览器访问配置页面：`http://192.168.4.1/`
- 配置 Wi-Fi、服务器地址、定时更新时间并保存，设备会自动重启并进入正常工作流程。

## 刷新与休眠

- 设备每天会在配置的更新时间，从服务器拉取一次当日生成的图片，并刷新墨水屏。
- 成功刷新后，会进入 Deep Sleep，直到下一次被唤醒。
- 若下载超时（默认 60s），也会进入长休眠，避免异常耗电。
- 在任意时候，按下 RESET 键，会强制重启并马上拉取、刷新一次图片。
- 长休眠待机电流 ＜ 1mA，如使用 2 节 18650 电池，5000mAh 约可实现半年续航。

## 相关项目
- ESP32 固件依赖 GxEPD2 © ZinggJM（GPL-3.0）：https://github.com/ZinggJM/GxEPD2  
  如对外分发编译后的固件，请同时遵守 GPL-3.0。


- 项目中的离线中文城市名索引，基于 GeoNames 数据制作：  
GeoNames © GeoNames contributors, CC BY 4.0  
https://www.geonames.org/