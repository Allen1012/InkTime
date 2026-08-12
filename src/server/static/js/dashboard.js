/**
 * dashboard 模板右侧信息栏
 *
 * 只负责信息栏；照片的加载、切换、屏幕常亮、界面自动隐藏都由 display.js 处理
 * （两个模板共用同一套 DOM id，所以 display.js 无需改动）。
 *
 * 数据来源分两类：
 * - 时钟：纯前端本地时间，每秒更新，不发任何请求
 * - 农历节气 / 历史上的今天：服务端 /api/panel，服务端已做缓存与降级
 */

// 面板数据刷新间隔：内容按天变化，30 分钟一次足够，兼顾跨零点自动更新
const PANEL_REFRESH_MS = 30 * 60 * 1000;
// 天气刷新间隔：天气按小时变化，30 分钟偏慢，单独走 10 分钟一轮。
// 服务端另有 TTL 缓存，这里加密不会等比放大对外部服务的请求量。
const WEATHER_REFRESH_MS = 10 * 60 * 1000;

let panelLastDate = null;   // 上次拿到的日期，用于检测跨零点

document.addEventListener('DOMContentLoaded', () => {
  try {
    startClock();
  } catch (e) {
    console.error('[dashboard] 时钟启动失败', e);
  }

  try {
    refreshPanel();
    setInterval(refreshPanel, PANEL_REFRESH_MS);
    setInterval(refreshWeatherOnly, WEATHER_REFRESH_MS);
  } catch (e) {
    console.error('[dashboard] 信息面板初始化失败', e);
  }
});

/* ==================== 时钟 ==================== */

function startClock() {
  tickClock();
  // 对齐到下一秒再开始按秒走，避免显示的秒数与系统时间差半秒
  setTimeout(() => {
    tickClock();
    setInterval(tickClock, 1000);
  }, 1000 - (Date.now() % 1000));
}

function tickClock() {
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');

  setText('dash-hhmm', `${pad(now.getHours())}:${pad(now.getMinutes())}`);
  setText('dash-ss', pad(now.getSeconds()));

  // 跨零点时日期和农历都要变，立即拉一次面板数据
  const iso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  if (panelLastDate && panelLastDate !== iso) {
    console.log('[dashboard] 检测到跨日，刷新信息面板');
    panelLastDate = iso;
    refreshPanel();
  }
}

/* ==================== 信息面板 ==================== */

async function refreshPanel() {
  let payload;
  try {
    const resp = await fetch('/api/panel');
    payload = await resp.json();
  } catch (e) {
    console.warn('[dashboard] 获取信息面板数据失败', e);
    setText('dash-panel-status', '信息面板离线');
    return;
  }

  if (!payload || payload.status !== 'ok' || !payload.data) {
    console.warn('[dashboard] 信息面板返回异常', payload);
    setText('dash-panel-status', '信息面板异常');
    return;
  }

  const d = payload.data;
  panelLastDate = d.date && d.date.iso;

  renderDate(d.date);
  renderWeather(d.weather);
  renderLunar(d.lunar);
  renderHistory(d.onthisday);

  setText('dash-panel-status', '');
}

/* ==================== 天气 ==================== */

/**
 * 只刷新天气
 *
 * 天气变化比农历和历史上的今天快得多，所以用比面板更密的节奏单独拉一次。
 * 复用同一个 /api/panel 接口：服务端对天气有独立 TTL 缓存，多拉几次不会等比
 * 放大对外部天气服务的请求量。
 */
async function refreshWeatherOnly() {
  try {
    const resp = await fetch('/api/panel');
    const payload = await resp.json();
    if (payload && payload.status === 'ok' && payload.data) {
      renderWeather(payload.data.weather);
    }
  } catch (e) {
    console.warn('[dashboard] 单独刷新天气失败', e);
  }
}

/**
 * 设置天气图标引用
 *
 * 同时写 href 与 xlink:href：href 是 SVG2 语法，旧版 WebKit 与老 Android 浏览器
 * 只认 xlink:href。展示页常跑在电子相框、旧平板这类老浏览器上，两个都写是廉价保险。
 */
function setWeatherIcon(element, name) {
  if (!element) return;
  const reference = '#wi-' + (name || 'cloud');
  element.setAttribute('href', reference);
  element.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', reference);
}

/**
 * 渲染天气块
 *
 * 不可用就整块隐藏，与「历史上的今天」一致：天气是唯一的外网依赖，
 * 它拿不到时页面应当安静地少一块，而不是显示报错或空壳。
 */
function renderWeather(weather) {
  const block = document.getElementById('dash-weather-block');
  if (!block) return;

  if (!weather || !weather.available) {
    block.hidden = true;
    if (weather && weather.error && weather.error !== 'weather_disabled') {
      console.warn('[dashboard] 天气不可用：' + weather.error);
    }
    return;
  }

  setWeatherIcon(document.getElementById('dash-weather-icon'), weather.icon);

  setText('dash-weather-temp', `${weather.temperature}°`);
  setText('dash-weather-text', weather.text || '');
  setText('dash-weather-place', weather.location_name || '');

  // 体感与实际温度相同时不重复显示，省一处噪音
  const parts = [];
  if (Number.isFinite(weather.apparent_temperature)
      && weather.apparent_temperature !== weather.temperature) {
    parts.push(`体感 ${weather.apparent_temperature}°`);
  }
  if (Number.isFinite(weather.humidity) && weather.humidity > 0) {
    parts.push(`湿度 ${weather.humidity}%`);
  }
  if (weather.wind_direction && Number.isFinite(weather.wind_level)) {
    parts.push(`${weather.wind_direction} ${weather.wind_level} 级`);
  }
  setText('dash-weather-detail', parts.join(' · '));

  // 陈旧数据明确标注获取时间，避免把几小时前的天气当成当前天气
  const stale = document.getElementById('dash-weather-stale');
  if (stale) {
    if (weather.stale && weather.fetched_at) {
      const at = new Date(weather.fetched_at * 1000);
      const pad = n => String(n).padStart(2, '0');
      stale.textContent = `数据来自 ${pad(at.getHours())}:${pad(at.getMinutes())}，暂时无法更新`;
      stale.hidden = false;
    } else {
      stale.hidden = true;
    }
  }

  block.hidden = false;
}

function renderDate(date) {
  if (!date) return;
  setText('dash-date', `${date.year} 年 ${date.month} 月 ${date.day} 日`);
  setText('dash-weekday', date.weekday || '');
}

function renderLunar(lunar) {
  const block = document.getElementById('dash-lunar-block');
  if (!lunar || !lunar.available) {
    // 农历是离线计算的，走到这里通常是依赖缺失
    if (block) block.hidden = true;
    if (lunar && lunar.error) console.warn('[dashboard] 农历不可用：' + lunar.error);
    return;
  }
  if (block) block.hidden = false;

  setText('dash-lunar-text', lunar.text || '');

  // 当日节气
  toggleBadge('dash-jieqi', lunar.jieqi);

  // 传统节日（春节、端午等）
  const festival = (lunar.festivals && lunar.festivals.length) ? lunar.festivals[0] : '';
  toggleBadge('dash-festival', festival);

  // 下一个节气还有几天
  const nj = lunar.next_jieqi;
  setText('dash-next-jieqi',
    nj ? `距 ${nj.name} 还有 ${nj.days_left} 天` : '');
}

function renderHistory(onthisday) {
  const block = document.getElementById('dash-history-block');
  const list = document.getElementById('dash-history-list');
  if (!block || !list) return;

  const items = (onthisday && onthisday.items) || [];
  if (!onthisday || !onthisday.available || items.length === 0) {
    // 外部数据源失败时整块隐藏，不留空标题
    block.hidden = true;
    if (onthisday && onthisday.error) {
      console.warn('[dashboard] 历史上的今天不可用：' + onthisday.error);
    }
    return;
  }

  block.hidden = false;
  list.innerHTML = '';
  items.forEach(it => {
    const li = document.createElement('li');
    const year = document.createElement('span');
    year.className = 'history-year';
    year.textContent = it.year != null ? it.year : '—';
    // 用 textContent 而非 innerHTML，避免外部文本里的标记被当成 HTML 解析
    const text = document.createTextNode(it.text || '');
    li.appendChild(year);
    li.appendChild(text);
    list.appendChild(li);
  });

  if (onthisday.stale) {
    setText('dash-panel-status', '历史数据来自缓存');
  }
}

/* ==================== 工具 ==================== */

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function toggleBadge(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (value) {
    el.textContent = value;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}
