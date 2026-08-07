// 纯展示页面脚本文件

// 全局变量
let currentPhoto = null;
let autoPlayTimer = null;
let isAutoPlay = true;
let allPhotos = [];
let currentPhotoIndex = 0;

// 自动切换配置，启动时从 /api/settings 读取（对应 .env 的 DISPLAY_ROTATE_* 项）
//   interval — 固定间隔切换
//   hourly   — 每到整点切换，与真实时钟对齐
//   minutely — 每到整分切换（调试用）
//   daily    — 每天 00:00 切换
//   off      — 不自动切换
let rotateConfig = { mode: 'interval', intervalSec: 60 };

// 与时钟对齐的模式：手动切换时不重置计时，否则会偏离整点
const ALIGNED_MODES = ['hourly', 'minutely', 'daily'];

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
  } catch (e) {
    console.warn('读取切换配置失败，使用默认值', rotateConfig, e);
  }
  console.log('[display] 自动切换配置', rotateConfig);
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
  // 加载所有照片
  await loadAllPhotos();
  
  if (allPhotos.length > 0) {
    // 显示今日日期
    updateDate();
    
    // 加载第一张照片
    loadPhotoByIndex(0);
  }
}

/**
 * 加载所有照片
 */
async function loadAllPhotos() {
  try {
    const response = await fetch('/api/photos');
    const data = await response.json();
    
    if (data.status === 'ok') {
      // API 返回的数据结构是 {"data": {"items": [...]}}
      allPhotos = data.data.items || [];
      console.log('加载了', allPhotos.length, '张照片');
    }
  } catch (error) {
    console.error('加载照片列表失败:', error);
  }
}

/**
 * 根据索引加载照片
 */
function loadPhotoByIndex(index) {
  if (allPhotos.length === 0) return;
  
  // 确保索引在有效范围内
  if (index < 0) {
    index = allPhotos.length - 1;
  } else if (index >= allPhotos.length) {
    index = 0;
  }
  
  currentPhotoIndex = index;
  const photo = allPhotos[currentPhotoIndex];
  
  if (photo) {
    currentPhoto = photo;
    renderPhoto(photo);
  }
}

/**
 * 加载照片
 * @param {number} photoId - 照片 ID（可选）
 */
async function loadPhoto(photoId = null) {
  // 显示加载动画
  showLoading();
  
  try {
    if (photoId) {
      // 从 API 获取指定照片
      const response = await fetch(`/api/photo/${photoId}`);
      const data = await response.json();
      
      if (data.status === 'ok') {
        currentPhoto = data.data;
        renderPhoto(currentPhoto);
      }
    } else {
      // 随机获取一张照片
      if (allPhotos.length > 0) {
        const randomIndex = Math.floor(Math.random() * allPhotos.length);
        currentPhoto = allPhotos[randomIndex];
        renderPhoto(currentPhoto);
      }
    }
  } catch (error) {
    console.error('加载照片失败:', error);
  } finally {
    // 隐藏加载动画
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
  // 重置自动播放
  resetAutoPlay();
  
  // 加载上一张
  loadPhotoByIndex(currentPhotoIndex - 1);
}

/**
 * 加载下一张照片
 */
function loadNextPhoto() {
  // 重置自动播放
  resetAutoPlay();
  
  // 加载下一张
  loadPhotoByIndex(currentPhotoIndex + 1);
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
 */
function scheduleNextRotate() {
  const delay = msUntilNextRotate();
  autoPlayTimer = setTimeout(() => {
    loadNextPhoto();
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
    autoPlayLabel.textContent = isAutoPlay ? modeText : `${modeText}（已暂停）`;
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