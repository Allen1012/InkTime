// 纯展示页面脚本文件

// 全局变量
let currentPhoto = null;
let autoPlayInterval = null;
let isAutoPlay = true;
let allPhotos = [];
let currentPhotoIndex = 0;
const AUTO_PLAY_INTERVAL = 60000; // 自动切换间隔（毫秒）

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化页面
  initDisplayPage();
  
  // 绑定事件
  bindEvents();
  
  // 启动自动切换
  startAutoPlay();
});

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
  
  // 渲染日期
  const dateElement = document.getElementById('display-date');
  if (dateElement) {
    dateElement.textContent = photo.date_taken ? formatDate(photo.date_taken) : '';
  }
  
  // 渲染地点
  const locationElement = document.getElementById('display-location');
  if (locationElement) {
    locationElement.textContent = photo.location || '';
  }
  
  // 更新页面标题
  document.title = `InkTime - ${photo.title}`;
}

/**
 * 格式化日期
 */
function formatDate(dateString) {
  if (!dateString) return '';
  const date = new Date(dateString);
  const options = {
    year: 'numeric',
    month: 'long',
    day: 'numeric'
  };
  return date.toLocaleDateString('zh-CN', options);
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
  if (autoPlayInterval) {
    clearInterval(autoPlayInterval);
  }
  
  autoPlayInterval = setInterval(() => {
    loadNextPhoto();
  }, AUTO_PLAY_INTERVAL);
  
  // 更新自动播放状态
  updateAutoPlayUI();
}

/**
 * 停止自动播放
 */
function stopAutoPlay() {
  if (autoPlayInterval) {
    clearInterval(autoPlayInterval);
    autoPlayInterval = null;
  }
  
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
  if (isAutoPlay) {
    startAutoPlay();
  }
}

/**
 * 更新自动播放 UI
 */
function updateAutoPlayUI() {
  const autoPlayToggle = document.querySelector('.auto-play-toggle');
  const autoPlayIndicator = document.querySelector('.auto-play-indicator');
  
  if (autoPlayToggle) {
    if (isAutoPlay) {
      autoPlayToggle.innerHTML = '<i class="fa fa-pause"></i>';
      autoPlayToggle.title = '暂停自动播放';
    } else {
      autoPlayToggle.innerHTML = '<i class="fa fa-play"></i>';
      autoPlayToggle.title = '开始自动播放';
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