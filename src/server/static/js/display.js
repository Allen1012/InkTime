// 纯展示页面脚本文件

// 全局变量
let currentPhoto = null;
let autoPlayInterval = null;
let isAutoPlay = true;
const AUTO_PLAY_INTERVAL = 10000; // 自动切换间隔（毫秒）

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
function initDisplayPage() {
  // 加载初始照片
  loadPhoto();
  
  // 显示今日日期
  updateDate();
}

/**
 * 加载照片
 * @param {number} photoId - 照片 ID（可选）
 */
async function loadPhoto(photoId = null) {
  // 显示加载动画
  showLoading();
  
  try {
    // 模拟 API 请求
    const photo = await mockFetchPhoto(photoId);
    
    if (photo) {
      currentPhoto = photo;
      renderPhoto(photo);
    }
  } catch (error) {
    console.error('加载照片失败:', error);
  } finally {
    // 隐藏加载动画
    hideLoading();
  }
}

/**
 * 模拟获取照片数据
 * @param {number} photoId - 照片 ID（可选）
 * @returns {Promise} 照片数据
 */
async function mockFetchPhoto(photoId = null) {
  // 模拟延迟
  await new Promise(resolve => setTimeout(resolve, 500));
  
  // 生成随机照片 ID
  const id = photoId || Math.floor(Math.random() * 100) + 1;
  
  // 模拟照片数据
  return {
    id: id,
    title: `照片 ${id}`,
    caption: `这是一张美丽的照片，展示了令人难忘的瞬间。`,
    image_url: `https://picsum.photos/800/600?random=${id}`,
    location: ['北京', '上海', '广州', '深圳', '杭州'][Math.floor(Math.random() * 5)],
    date_taken: new Date(Date.now() - Math.random() * 365 * 24 * 60 * 60 * 1000).toISOString()
  };
}

/**
 * 渲染照片
 * @param {Object} photo - 照片数据
 */
function renderPhoto(photo) {
  // 渲染照片
  const photoElement = document.getElementById('display-photo');
  if (photoElement) {
    photoElement.src = photo.image_url;
    photoElement.alt = photo.title;
    // 添加淡入动画
    photoElement.classList.add('fade-in');
    setTimeout(() => {
      photoElement.classList.remove('fade-in');
    }, 500);
  }
  
  // 渲染文字
  const captionElement = document.getElementById('display-caption');
  if (captionElement) {
    captionElement.textContent = photo.caption;
  }
  
  // 渲染地点
  const locationElement = document.getElementById('display-location');
  if (locationElement) {
    locationElement.textContent = photo.location || '未知地点';
  }
  
  // 更新页面标题
  document.title = `InkTime - ${photo.title}`;
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
async function loadPreviousPhoto() {
  // 重置自动播放
  resetAutoPlay();
  
  // 计算上一张照片 ID
  const prevId = currentPhoto ? (currentPhoto.id > 1 ? currentPhoto.id - 1 : 100) : 100;
  await loadPhoto(prevId);
}

/**
 * 加载下一张照片
 */
async function loadNextPhoto() {
  // 重置自动播放
  resetAutoPlay();
  
  // 计算下一张照片 ID
  const nextId = currentPhoto ? (currentPhoto.id < 100 ? currentPhoto.id + 1 : 1) : 1;
  await loadPhoto(nextId);
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