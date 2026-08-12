// 纯展示页面脚本文件

// 全局变量
let currentPhoto = null;
let autoPlayTimer = null;
let isAutoPlay = true;
// 已展示照片的历史栈。往回翻不请求服务端、不消耗展示次数
let photoHistory = [];
let historyIndex = -1;
const HISTORY_MAX = 50;

// 自动切换配置，启动时从 /api/settings 读取（对应 .env 的 DISPLAY_ROTATE_* 项）
//   interval — 固定间隔切换
//   hourly   — 每到整点切换，与真实时钟对齐
//   minutely — 每到整分切换（调试用）
//   daily    — 每天 00:00 切换
//   off      — 不自动切换
let rotateConfig = { mode: 'interval', intervalSec: 60, keepAwake: true, uiHideDelaySec: 3 };
// 天气角标刷新间隔：只在沉浸式模板且开关打开时启用
const WEATHER_CORNER_REFRESH_MS = 10 * 60 * 1000;

// 操作界面自动隐藏相关
let uiHideTimer = null;
let uiPinned = false;   // 鼠标悬停在指示器上时置位，避免正要点击时界面消失

// 屏幕常亮锁状态：null 未尝试 / 'active' 已生效 / 其他为失败原因（展示在右上角）
let wakeLockSentinel = null;
let wakeLockState = null;

// 与时钟对齐的模式：手动切换时不重置计时，否则会偏离整点
const ALIGNED_MODES = ['hourly', 'minutely', 'daily'];

// 休息态：服务端判定当前不在生效时间段内。此期间不切换照片，
// 只按服务端给的退避间隔轮询，等待生效时间段开始
let isIdle = false;
let idleBackoffMs = 0;

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', async function() {
  // 各步骤独立 try/catch：任一步失败都不能拖累后续。
  // 尤其 bindEvents 与 startAutoPlay 必须执行，否则按钮点了没反应、
  // 也不会自动切换，且现象很难和「配置没生效」区分开。
  try {
    await loadRotateConfig();
  } catch (e) {
    console.error('[display] 读取切换配置失败，使用默认值', e);
  }

  try {
    await initDisplayPage();
  } catch (e) {
    console.error('[display] 初始化页面失败', e);
  }

  try {
    bindEvents();
  } catch (e) {
    console.error('[display] 绑定事件失败', e);
  }

  try {
    startAutoPlay();
  } catch (e) {
    console.error('[display] 启动自动切换失败', e);
  }

  try {
    await requestWakeLock();
  } catch (e) {
    console.error('[display] 请求屏幕常亮失败', e);
  }

  try {
    initUiAutoHide();
  } catch (e) {
    console.error('[display] 初始化界面自动隐藏失败', e);
  }
});

/**
 * 操作界面自动隐藏
 *
 * 静置 DISPLAY_UI_HIDE_DELAY_SEC 秒后给容器加 .ui-hidden，
 * 由 CSS 淡出右上角指示器、左右切换提示，并隐藏鼠标光标；
 * 鼠标移动 / 按键 / 滚轮 / 触摸时立即恢复并重新计时。
 *
 * 底部文案区不在隐藏范围内 —— 那是照片内容，不是操作控件。
 */
function initUiAutoHide() {
  const container = document.querySelector('.display-container');
  if (!container) return;

  if (rotateConfig.uiHideDelaySec <= 0) {
    console.log('[display] DISPLAY_UI_HIDE_DELAY_SEC=0，界面不自动隐藏');
    return;
  }

  // 鼠标停在指示器上时不隐藏，否则正要点暂停按钮时界面会消失
  const indicator = document.querySelector('.auto-play-indicator');
  if (indicator) {
    indicator.addEventListener('mouseenter', () => {
      uiPinned = true;
      showUi();
    });
    indicator.addEventListener('mouseleave', () => {
      uiPinned = false;
      scheduleUiHide();
    });
  }

  ['mousemove', 'mousedown', 'wheel', 'keydown', 'touchstart'].forEach(evt => {
    document.addEventListener(evt, showUi, { passive: true });
  });

  // 进入页面后先正常显示，静置到时间再隐藏
  scheduleUiHide();
}

function showUi() {
  const container = document.querySelector('.display-container');
  if (container) container.classList.remove('ui-hidden');
  scheduleUiHide();
}

function scheduleUiHide() {
  if (uiHideTimer) {
    clearTimeout(uiHideTimer);
    uiHideTimer = null;
  }
  if (rotateConfig.uiHideDelaySec <= 0 || uiPinned) return;

  uiHideTimer = setTimeout(() => {
    const container = document.querySelector('.display-container');
    if (container && !uiPinned) container.classList.add('ui-hidden');
  }, rotateConfig.uiHideDelaySec * 1000);
}

/**
 * 请求屏幕常亮锁，阻止系统空闲息屏 / 锁屏
 *
 * Screen Wake Lock API 底层走的是系统的 idle inhibit 机制（GNOME 下等价于
 * gnome-session-inhibit --inhibit idle），属于系统设计支持的行为。
 *
 * 两个前提容易踩坑：
 * 1. 必须是安全上下文。用局域网 IP 的 http 访问时 navigator.wakeLock 直接是
 *    undefined，必须用 http://127.0.0.1:<端口>/display 或 HTTPS。
 * 2. 标签页切到后台时浏览器会自动释放锁，需要在 visibilitychange 时重新请求。
 */
async function requestWakeLock() {
  if (!rotateConfig.keepAwake) {
    wakeLockState = null;
    return;
  }

  if (!('wakeLock' in navigator)) {
    wakeLockState = window.isSecureContext
      ? '浏览器不支持常亮'
      : '需用 127.0.0.1 访问';
    console.warn(
      '[display] 无法阻止息屏：' +
      (window.isSecureContext
        ? '当前浏览器不支持 Screen Wake Lock API'
        : '当前不是安全上下文，请用 http://127.0.0.1:<端口>/display 或 HTTPS 访问')
    );
    updateAutoPlayUI();
    return;
  }

  // 已持有且未释放时不重复请求
  if (wakeLockSentinel && !wakeLockSentinel.released) return;

  try {
    wakeLockSentinel = await navigator.wakeLock.request('screen');
    wakeLockState = 'active';
    console.log('[display] 已获取屏幕常亮锁');
    wakeLockSentinel.addEventListener('release', () => {
      console.log('[display] 屏幕常亮锁已释放（切到后台或系统回收）');
      wakeLockState = '常亮已释放';
      updateAutoPlayUI();
    });
  } catch (e) {
    // 常见原因：系统省电模式、电量过低、策略禁止
    wakeLockState = '常亮被拒绝';
    console.warn('[display] 获取屏幕常亮锁失败：' + (e && e.message), e);
  }

  updateAutoPlayUI();
}

// 标签页重新可见时补回常亮锁（浏览器会在页面隐藏时自动释放）
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    requestWakeLock().catch(e => console.error('[display] 重新请求常亮失败', e));
  }
});

/**
 * 从后端读取自动切换配置
 */
async function loadRotateConfig() {
  try {
    const resp = await fetch('/api/settings');
    const data = await resp.json();
    if (data && data.display_rotate_mode) {
      rotateConfig.mode = String(data.display_rotate_mode).toLowerCase();
    }
    const sec = Number(data && data.display_rotate_interval_sec);
    if (Number.isFinite(sec) && sec > 0) {
      rotateConfig.intervalSec = sec;
    }
    if (data && typeof data.display_keep_awake === 'boolean') {
      rotateConfig.keepAwake = data.display_keep_awake;
    }
    const hideSec = Number(data && data.display_ui_hide_delay_sec);
    if (Number.isFinite(hideSec) && hideSec >= 0) {
      rotateConfig.uiHideDelaySec = hideSec;
    }
  } catch (e) {
    console.warn('读取切换配置失败，使用默认值', rotateConfig, e);
  }
  console.log('[display] 自动切换配置', rotateConfig);
}

/* ==================== 天气角标（仅沉浸式模板） ==================== */

/**
 * 启动天气角标
 *
 * 只在两个条件同时成立时才发请求：页面里有角标元素（dashboard 模板没有，它由
 * dashboard.js 渲染完整天气块），以及 DISPLAY_WEATHER_CORNER 已打开。
 * 这样沉浸式模板默认不会因为天气功能多出任何请求。
 */
function startWeatherCorner() {
  // 元素只在沉浸式模板且 DISPLAY_WEATHER_CORNER 打开时由服务端渲染出来，
  // 因此它不存在就意味着不需要天气，连一次请求都不会发。
  const box = document.getElementById('display-weather');
  if (!box) return;
  refreshWeatherCorner();
  setInterval(refreshWeatherCorner, WEATHER_CORNER_REFRESH_MS);
}

/**
 * 拉取并渲染角标天气，失败或不可用时整块隐藏
 */
async function refreshWeatherCorner() {
  const box = document.getElementById('display-weather');
  if (!box) return;
  try {
    const resp = await fetch('/api/panel');
    const payload = await resp.json();
    const weather = payload && payload.data && payload.data.weather;
    if (!weather || !weather.available) {
      box.hidden = true;
      return;
    }
    const icon = document.getElementById('display-weather-icon');
    if (icon) icon.setAttribute('href', '#wi-' + (weather.icon || 'cloud'));
    const temp = document.getElementById('display-weather-temp');
    if (temp) temp.textContent = `${weather.temperature}°`;
    box.hidden = false;
  } catch (e) {
    console.warn('[display] 获取天气失败', e);
    box.hidden = true;
  }
}

/**
 * 计算距离下一次切换的毫秒数
 */
function msUntilNextRotate() {
  const now = new Date();
  switch (rotateConfig.mode) {
    case 'hourly': {
      const next = new Date(now);
      next.setMinutes(0, 0, 0);
      next.setHours(now.getHours() + 1);
      return next.getTime() - now.getTime();
    }
    case 'minutely': {
      const next = new Date(now);
      next.setSeconds(0, 0);
      next.setMinutes(now.getMinutes() + 1);
      return next.getTime() - now.getTime();
    }
    case 'daily': {
      const next = new Date(now);
      next.setHours(0, 0, 0, 0);
      next.setDate(now.getDate() + 1);
      return next.getTime() - now.getTime();
    }
    case 'interval':
    default:
      return rotateConfig.intervalSec * 1000;
  }
}

/**
 * 初始化展示页面
 */
async function initDisplayPage() {
  // 显示今日日期
  updateDate();

  // 天气角标：仅沉浸式模板且开关打开时启用
  try {
    startWeatherCorner();
  } catch (e) {
    console.warn('[display] 天气角标初始化失败', e);
  }

  // 取第一张照片
  await loadNextFromServer();
}

/**
 * 向服务端请求下一张照片
 *
 * 选片算法在服务端（gallery.py）：只从「当前最小展示次数」的照片中加权随机选，
 * 保证每张都能被看到、新照片不霸屏，选中即记账。
 *
 * 因为每次都实时查库，新分析入库的照片会自动进入候选，
 * 前端不需要持有全量列表、也不需要定时重新拉取。
 */
async function loadNextFromServer() {
  try {
    const url = currentPhoto && currentPhoto.id
      ? `/api/display/next?exclude=${encodeURIComponent(currentPhoto.id)}`
      : '/api/display/next';
    const resp = await fetch(url);
    const data = await resp.json();

    // 休息期：服务端判定当前不在生效时间段内。此时不切换、不记账，
    // 前端只负责渲染服务端给的画面，并改用退避轮询等待恢复。
    if (data.status === 'idle') {
      applyIdleState(data);
      return currentPhoto;
    }

    if (data.status !== 'ok' || !data.data) {
      console.warn('[display] 取照片失败:', data.message || data);
      return null;
    }

    clearIdleState();

    if (data.stats) {
      const s = data.stats;
      console.log(`[display] 第 ${s.round} 轮，本轮剩余 ${s.remaining_in_round}/${s.pool_total} 张`
        + (s.newly_added ? `，新纳入 ${s.newly_added} 张` : ''));
    }

    pushHistory(data.data);
    currentPhoto = data.data;
    renderPhoto(currentPhoto);
    return currentPhoto;
  } catch (e) {
    console.error('[display] 请求下一张照片失败', e);
    return null;
  } finally {
    hideLoading();
  }
}

/**
 * 进入或维持休息态
 *
 * 服务端已按生效时间段判定，前端不做时间判断：展示设备的系统时间和时区常常不准，
 * 一旦由前端判断，时区差一小时休息时段就整体偏移。
 *
 * freeze 与 photo 模式下服务端会带回一张照片，直接复用正常渲染；rest 模式下没有
 * 照片，改为显示休息文案。按需求约定，休息期不展示恢复时间。
 */
function applyIdleState(payload) {
  const backoffSec = Number(payload.next_check_after_sec);
  idleBackoffMs = Number.isFinite(backoffSec) && backoffSec > 0
    ? backoffSec * 1000
    : 300000;

  if (payload.data) {
    hideRestOverlay();
    // 同一张照片不重复渲染，避免休息期反复触发图片加载
    if (!currentPhoto || currentPhoto.id !== payload.data.id) {
      currentPhoto = payload.data;
      renderPhoto(currentPhoto);
    }
  } else {
    showRestOverlay(payload.message || '休息中');
  }

  if (!isIdle) {
    isIdle = true;
    console.log('[display] 进入休息期，下次检查', idleBackoffMs / 1000, '秒后');
  }
  updateAutoPlayUI();
}

/**
 * 退出休息态，恢复正常轮播节奏
 */
function clearIdleState() {
  if (!isIdle) return;
  isIdle = false;
  idleBackoffMs = 0;
  hideRestOverlay();
  console.log('[display] 生效时间段已开始，恢复自动切换');
  updateAutoPlayUI();
}

/**
 * 显示休息遮罩
 */
function showRestOverlay(text) {
  const overlay = document.getElementById('display-rest');
  if (!overlay) return;
  const label = document.getElementById('display-rest-text');
  if (label) label.textContent = text;
  overlay.classList.add('is-visible');
}

/**
 * 隐藏休息遮罩
 */
function hideRestOverlay() {
  const overlay = document.getElementById('display-rest');
  if (overlay) overlay.classList.remove('is-visible');
}

/**
 * 历史栈：往回翻只是重放已看过的照片，不请求服务端、不消耗展示次数，
 * 因此不会污染轮次统计。
 */
function pushHistory(photo) {
  // 若当前不在栈顶（用户翻回去过），丢弃前面的分支再追加
  if (historyIndex < photoHistory.length - 1) {
    photoHistory = photoHistory.slice(0, historyIndex + 1);
  }
  photoHistory.push(photo);
  // 限长，避免长期运行内存无限增长
  if (photoHistory.length > HISTORY_MAX) {
    photoHistory.shift();
  }
  historyIndex = photoHistory.length - 1;
}

/**
 * 加载指定照片
 *
 * 只用于 /display/<id> 这种指定照片的场景。不传 id 时退回正常选片流程。
 * 注意：指定 id 的照片不计入展示次数，避免手动查看污染轮次统计。
 * @param {number} photoId - 照片 ID（可选）
 */
async function loadPhoto(photoId = null) {
  if (!photoId) {
    await loadNextFromServer();
    return;
  }

  showLoading();
  try {
    const response = await fetch(`/api/photo/${photoId}`);
    const data = await response.json();
    if (data.status === 'ok') {
      currentPhoto = data.data;
      pushHistory(currentPhoto);
      renderPhoto(currentPhoto);
    } else {
      console.warn('[display] 指定照片加载失败:', data.message || data);
    }
  } catch (error) {
    console.error('[display] 加载照片失败:', error);
  } finally {
    hideLoading();
  }
}

/**
 * 渲染照片
 * @param {Object} photo - 照片数据
 */
function renderPhoto(photo) {
  console.log('渲染照片:', photo);
  
  // 渲染照片
  const photoElement = document.getElementById('display-photo');
  if (photoElement) {
    // 使用 full_url 字段
    photoElement.src = photo.full_url;
    photoElement.alt = photo.title;
    console.log('照片 URL:', photo.full_url);
    // 添加淡入动画
    photoElement.classList.add('fade-in');
    setTimeout(() => {
      photoElement.classList.remove('fade-in');
    }, 500);
  }
  
  // 渲染文字（使用 side_caption）
  const captionElement = document.getElementById('display-caption');
  if (captionElement) {
    captionElement.textContent = photo.side_caption || '';
  }
  
  // 渲染日期（优先使用 EXIF 拍摄时间）
  const dateElement = document.getElementById('display-date');
  const dateContainer = dateElement ? dateElement.parentElement : null;
  
  // 优先使用 EXIF 拍摄时间，其次使用 date_taken
  const photoDate = photo.exif_data && photo.exif_data['拍摄时间'] 
    ? photo.exif_data['拍摄时间'] 
    : photo.date_taken;
  
  if (dateElement && photoDate) {
    dateElement.textContent = formatDate(photoDate);
    if (dateContainer) dateContainer.style.display = 'flex';
  } else {
    if (dateContainer) dateContainer.style.display = 'none';
  }
  
  // 渲染地点（如果有数据）
  const locationElement = document.getElementById('display-location');
  const locationContainer = locationElement ? locationElement.parentElement : null;
  if (locationElement && photo.location) {
    locationElement.textContent = photo.location;
    if (locationContainer) locationContainer.style.display = 'flex';
  } else {
    if (locationContainer) locationContainer.style.display = 'none';
  }
  
  // 更新页面标题
  document.title = `InkTime - ${photo.title}`;
}

/**
 * 格式化日期
 */
function formatDate(dateString) {
  if (!dateString || dateString.trim() === '') return '';
  
  try {
    let date;
    
    // 尝试解析EXIF格式日期 (YYYY:MM:DD HH:MM:SS)
    if (dateString.includes(':') && dateString.match(/^\d{4}:\d{2}:\d{2}/)) {
      const parts = dateString.split(' ');
      if (parts.length >= 2) {
        const dateParts = parts[0].split(':');
        const timeParts = parts[1].split(':');
        if (dateParts.length === 3 && timeParts.length >= 2) {
          // 构建标准日期字符串
          const standardDate = `${dateParts[0]}-${dateParts[1]}-${dateParts[2]} ${timeParts[0]}:${timeParts[1]}`;
          date = new Date(standardDate);
        }
      }
    }
    
    // 如果EXIF格式解析失败，尝试标准格式
    if (!date || isNaN(date.getTime())) {
      date = new Date(dateString);
    }
    
    // 检查日期是否有效
    if (isNaN(date.getTime())) {
      console.error('Invalid date:', dateString);
      return dateString; // 返回原始字符串
    }
    
    const options = {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    };
    return date.toLocaleDateString('zh-CN', options);
  } catch (error) {
    console.error('日期格式化错误:', error, dateString);
    return dateString; // 返回原始字符串
  }
}

/**
 * 更新今日日期
 */
function updateDate() {
  const dateElement = document.getElementById('display-date');
  if (dateElement) {
    const today = new Date();
    const options = {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      weekday: 'long'
    };
    dateElement.textContent = today.toLocaleDateString('zh-CN', options);
  }
}

/**
 * 绑定事件
 */
function bindEvents() {
  // 点击页面跳转到详情页
  const displayContainer = document.querySelector('.display-container');
  if (displayContainer) {
    displayContainer.addEventListener('click', function(e) {
      // 阻止点击切换按钮时的默认行为
      if (e.target.closest('.navigation-hint') || e.target.closest('.auto-play-toggle')) {
        return;
      }
      
      // 跳转到详情页
      if (currentPhoto) {
        window.location.href = `/photo/${currentPhoto.id}`;
      }
    });
  }
  
  // 左切换按钮
  const leftHint = document.querySelector('.navigation-hint.left');
  if (leftHint) {
    leftHint.addEventListener('click', function(e) {
      e.stopPropagation();
      loadPreviousPhoto();
    });
  }
  
  // 右切换按钮
  const rightHint = document.querySelector('.navigation-hint.right');
  if (rightHint) {
    rightHint.addEventListener('click', function(e) {
      e.stopPropagation();
      loadNextPhoto();
    });
  }
  
  // 自动播放切换按钮
  const autoPlayToggle = document.querySelector('.auto-play-toggle');
  if (autoPlayToggle) {
    autoPlayToggle.addEventListener('click', function(e) {
      e.stopPropagation();
      toggleAutoPlay();
    });
  }
  
  // 键盘事件
  document.addEventListener('keydown', function(e) {
    switch (e.key) {
      case 'ArrowLeft':
        loadPreviousPhoto();
        break;
      case 'ArrowRight':
        loadNextPhoto();
        break;
      case ' ': // 空格键
        toggleAutoPlay();
        break;
    }
  });
  
  // 触摸事件（滑动切换）
  let touchStartX = 0;
  let touchEndX = 0;
  
  document.addEventListener('touchstart', function(e) {
    touchStartX = e.changedTouches[0].screenX;
  });
  
  document.addEventListener('touchend', function(e) {
    touchEndX = e.changedTouches[0].screenX;
    handleSwipe();
  });
  
  function handleSwipe() {
    const swipeThreshold = 50;
    if (touchEndX < touchStartX - swipeThreshold) {
      // 向左滑动，显示下一张
      loadNextPhoto();
    } else if (touchEndX > touchStartX + swipeThreshold) {
      // 向右滑动，显示上一张
      loadPreviousPhoto();
    }
  }
}

/**
 * 加载上一张照片
 */
function loadPreviousPhoto() {
  resetAutoPlay();

  // 往回翻只重放历史，不请求服务端、不计数
  if (historyIndex > 0) {
    historyIndex -= 1;
    currentPhoto = photoHistory[historyIndex];
    renderPhoto(currentPhoto);
  } else {
    console.log('[display] 已经是历史中最早的一张');
  }
}

/**
 * 加载下一张照片
 */
function loadNextPhoto() {
  resetAutoPlay();

  // 若之前往回翻过，先在历史里前进，走到栈顶再向服务端要新的
  if (historyIndex < photoHistory.length - 1) {
    historyIndex += 1;
    currentPhoto = photoHistory[historyIndex];
    renderPhoto(currentPhoto);
    return;
  }

  loadNextFromServer();
}

/**
 * 启动自动播放
 */
function startAutoPlay() {
  clearAutoPlayTimer();

  if (rotateConfig.mode === 'off') {
    console.log('[display] DISPLAY_ROTATE_MODE=off，不自动切换');
    updateAutoPlayUI();
    return;
  }

  scheduleNextRotate();
  updateAutoPlayUI();
}

/**
 * 调度下一次切换
 *
 * 用递归 setTimeout 而不是 setInterval：对齐模式每次都重新计算到下一个
 * 时钟边界的延迟，不会因为定时器误差累积而逐渐偏离整点。
 *
 * 休息期改用服务端给的退避间隔（上限五分钟），既不再按轮播节奏打扰服务端，
 * 又能在生效时间段开始或管理员改配置后自动恢复。
 */
function scheduleNextRotate() {
  const delay = isIdle && idleBackoffMs > 0 ? idleBackoffMs : msUntilNextRotate();
  autoPlayTimer = setTimeout(() => {
    if (isIdle) {
      // 休息期只向服务端确认是否已恢复，不走历史栈前进逻辑
      loadNextFromServer();
    } else {
      loadNextPhoto();
    }
    if (isAutoPlay && rotateConfig.mode !== 'off') {
      scheduleNextRotate();
    }
  }, delay);
}

/**
 * 清理定时器
 */
function clearAutoPlayTimer() {
  if (autoPlayTimer) {
    clearTimeout(autoPlayTimer);
    autoPlayTimer = null;
  }
}

/**
 * 停止自动播放
 */
function stopAutoPlay() {
  clearAutoPlayTimer();
  
  // 更新自动播放状态
  updateAutoPlayUI();
}

/**
 * 切换自动播放状态
 */
function toggleAutoPlay() {
  isAutoPlay = !isAutoPlay;
  
  if (isAutoPlay) {
    startAutoPlay();
  } else {
    stopAutoPlay();
  }
}

/**
 * 重置自动播放
 */
function resetAutoPlay() {
  if (!isAutoPlay) return;

  // 对齐模式（整点/整分/每天）下手动切换后不重新计时，
  // 否则下一次切换会偏离时钟边界，失去「整点切换」的意义。
  if (ALIGNED_MODES.includes(rotateConfig.mode)) return;

  startAutoPlay();
}

/**
 * 更新自动播放 UI
 */
function updateAutoPlayUI() {
  const autoPlayToggle = document.querySelector('.auto-play-toggle');
  const autoPlayIndicator = document.querySelector('.auto-play-indicator');
  const autoPlayLabel = document.getElementById('auto-play-label');

  const modeText = {
    hourly: '整点切换',
    minutely: '整分切换',
    daily: '每天切换',
    off: '不自动切换',
    interval: `每 ${rotateConfig.intervalSec} 秒切换`
  }[rotateConfig.mode] || '自动切换';

  // 直接把模式显示在右上角，不必悬停就能确认配置是否生效
  if (autoPlayLabel) {
    let text = isAutoPlay ? modeText : `${modeText}（已暂停）`;
    // 休息期明确标注，避免被误认为轮播卡死
    if (isIdle) text = `${modeText}（休息中）`;
    // 常亮状态一并显示：生效显示「· 常亮」，失败显示具体原因，便于排查
    if (wakeLockState === 'active') {
      text += ' · 常亮';
    } else if (wakeLockState) {
      text += ` · ${wakeLockState}`;
    }
    autoPlayLabel.textContent = text;
  }

  if (autoPlayToggle) {
    if (isAutoPlay) {
      autoPlayToggle.innerHTML = '<i class="fa fa-pause"></i>';
      autoPlayToggle.title = `暂停自动播放（当前：${modeText}）`;
    } else {
      autoPlayToggle.innerHTML = '<i class="fa fa-play"></i>';
      autoPlayToggle.title = `开始自动播放（当前：${modeText}）`;
    }
  }
  
  if (autoPlayIndicator) {
    if (isAutoPlay) {
      autoPlayIndicator.classList.add('active');
    } else {
      autoPlayIndicator.classList.remove('active');
    }
  }
}

/**
 * 显示加载动画
 */
function showLoading() {
  const loadingElement = document.querySelector('.loading');
  if (loadingElement) {
    loadingElement.style.display = 'flex';
  }
}

/**
 * 隐藏加载动画
 */
function hideLoading() {
  const loadingElement = document.querySelector('.loading');
  if (loadingElement) {
    loadingElement.style.display = 'none';
  }
}

/**
 * 处理错误
 * @param {Error} error - 错误对象
 */
function handleError(error) {
  console.error('错误:', error);
  
  // 显示错误消息
  const errorElement = document.createElement('div');
  errorElement.className = 'error-message';
  errorElement.textContent = '加载失败，请重试';
  errorElement.style.position = 'absolute';
  errorElement.style.bottom = '20px';
  errorElement.style.left = '50%';
  errorElement.style.transform = 'translateX(-50%)';
  errorElement.style.background = 'rgba(255, 0, 0, 0.8)';
  errorElement.style.color = '#fff';
  errorElement.style.padding = '10px 20px';
  errorElement.style.borderRadius = '5px';
  errorElement.style.zIndex = '100';
  
  document.body.appendChild(errorElement);
  
  // 3秒后移除错误消息
  setTimeout(() => {
    errorElement.remove();
  }, 3000);
}