// 主脚本文件

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化导航栏
  initNavbar();
  
  // 初始化图片懒加载
  initLazyLoad();
  
  // 初始化响应式调整
  initResponsiveAdjustments();
  
  // 初始化无障碍功能
  initAccessibility();
  
  // 初始化 Bootstrap tooltips
  initTooltips();
});

// 初始化 Bootstrap tooltips
function initTooltips() {
  // 使用事件委托来处理动态生成的元素
  document.body.addEventListener('mouseover', function(e) {
    if (e.target.closest('[data-bs-toggle="tooltip"]')) {
      const tooltipElement = e.target.closest('[data-bs-toggle="tooltip"]');
      let tooltip = bootstrap.Tooltip.getInstance(tooltipElement);
      if (!tooltip) {
        tooltip = new bootstrap.Tooltip(tooltipElement);
      }
    }
  });
}

// 初始化导航栏
function initNavbar() {
  // 导航栏滚动效果
  window.addEventListener('scroll', function() {
    const navbar = document.querySelector('.navbar');
    if (window.scrollY > 50) {
      navbar.classList.add('bg-light', 'shadow-sm');
    } else {
      navbar.classList.remove('bg-light', 'shadow-sm');
    }
  });
  
  // 移动端导航栏展开/收起
  const navbarToggler = document.querySelector('.navbar-toggler');
  const navbarCollapse = document.querySelector('.navbar-collapse');
  
  if (navbarToggler && navbarCollapse) {
    navbarToggler.addEventListener('click', function() {
      navbarCollapse.classList.toggle('show');
    });
  }
}

// 初始化图片懒加载
function initLazyLoad() {
  // 检查浏览器是否支持 Intersection Observer API
  if ('IntersectionObserver' in window) {
    const imageObserver = new IntersectionObserver(function(entries, observer) {
      entries.forEach(function(entry) {
        if (entry.isIntersecting) {
          const image = entry.target;
          image.src = image.dataset.src;
          image.classList.remove('lazy');
          imageObserver.unobserve(image);
        }
      });
    });
    
    const lazyImages = document.querySelectorAll('img[data-src]');
    lazyImages.forEach(function(image) {
      imageObserver.observe(image);
    });
  } else {
    // 不支持 Intersection Observer API 的浏览器回退方案
    const lazyImages = document.querySelectorAll('img[data-src]');
    lazyImages.forEach(function(image) {
      image.src = image.dataset.src;
      image.classList.remove('lazy');
    });
  }
}

// 初始化响应式调整
function initResponsiveAdjustments() {
  // 处理窗口大小变化
  function handleResize() {
    // 调整相册网格列数
    adjustPhotoGridColumns();
    
    // 调整导航栏
    adjustNavbar();
  }
  
  // 初始调用
  handleResize();
  
  // 窗口大小变化时调用
  window.addEventListener('resize', handleResize);
}

// 调整相册网格列数
function adjustPhotoGridColumns() {
  const photoGrid = document.getElementById('photo-grid');
  if (!photoGrid) return;
  
  const screenWidth = window.innerWidth;
  let columnClass = 'col-12';
  
  if (screenWidth >= 1200) {
    columnClass = 'col-xl-2 col-lg-3 col-md-4 col-sm-6';
  } else if (screenWidth >= 992) {
    columnClass = 'col-lg-3 col-md-4 col-sm-6';
  } else if (screenWidth >= 768) {
    columnClass = 'col-md-4 col-sm-6';
  } else if (screenWidth >= 576) {
    columnClass = 'col-sm-6';
  }
  
  const photoCards = photoGrid.querySelectorAll('.photo-card-container');
  photoCards.forEach(function(card) {
    card.className = 'photo-card-container ' + columnClass;
  });
}

// 调整导航栏
function adjustNavbar() {
  // 不再动态修改导航栏内容，避免重复文字
}

// 初始化无障碍功能
function initAccessibility() {
  // 为所有图片添加 alt 属性
  const images = document.querySelectorAll('img');
  images.forEach(function(image) {
    if (!image.alt) {
      image.alt = '照片';
    }
  });
  
  // 为所有按钮添加 aria-label
  const buttons = document.querySelectorAll('button');
  buttons.forEach(function(button) {
    if (!button.getAttribute('aria-label')) {
      button.setAttribute('aria-label', button.textContent.trim());
    }
  });
  
  // 为筛选和排序按钮添加无障碍支持
  const filterButtons = document.querySelectorAll('[data-filter]');
  filterButtons.forEach(function(button) {
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', button.classList.contains('active'));
    button.addEventListener('click', function() {
      filterButtons.forEach(function(btn) {
        btn.setAttribute('aria-checked', btn === button);
      });
    });
  });
  
  const sortButtons = document.querySelectorAll('[data-sort]');
  sortButtons.forEach(function(button) {
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', button.classList.contains('active'));
    button.addEventListener('click', function() {
      sortButtons.forEach(function(btn) {
        btn.setAttribute('aria-checked', btn === button);
      });
    });
  });
}

// 获取静态资源路径
function getStaticPath(path) {
  return '/static/' + path;
}

// 显示加载动画
function showLoading() {
  const loading = document.getElementById('loading');
  if (loading) {
    loading.classList.add('show');
  }
}

// 隐藏加载动画
function hideLoading() {
  const loading = document.getElementById('loading');
  if (loading) {
    loading.classList.remove('show');
  }
}

// 生成分页
function generatePagination(totalPages, currentPage, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  
  container.innerHTML = '';
  
  // 上一页按钮
  const prevItem = document.createElement('li');
  prevItem.className = 'page-item ' + (currentPage === 1 ? 'disabled' : '');
  prevItem.innerHTML = '<a class="page-link" href="#" aria-label="上一页"><span aria-hidden="true">&laquo;</span></a>';
  prevItem.querySelector('a').addEventListener('click', function(e) {
    e.preventDefault();
    if (currentPage > 1) {
      loadPage(currentPage - 1);
    }
  });
  container.appendChild(prevItem);
  
  // 页码按钮
  const startPage = Math.max(1, currentPage - 2);
  const endPage = Math.min(totalPages, startPage + 4);
  
  for (let i = startPage; i <= endPage; i++) {
    const pageItem = document.createElement('li');
    pageItem.className = 'page-item ' + (i === currentPage ? 'active' : '');
    pageItem.innerHTML = '<a class="page-link" href="#">' + i + '</a>';
    pageItem.querySelector('a').addEventListener('click', function(e) {
      e.preventDefault();
      loadPage(i);
    });
    container.appendChild(pageItem);
  }
  
  // 下一页按钮
  const nextItem = document.createElement('li');
  nextItem.className = 'page-item ' + (currentPage === totalPages ? 'disabled' : '');
  nextItem.innerHTML = '<a class="page-link" href="#" aria-label="下一页"><span aria-hidden="true">&raquo;</span></a>';
  nextItem.querySelector('a').addEventListener('click', function(e) {
    e.preventDefault();
    if (currentPage < totalPages) {
      loadPage(currentPage + 1);
    }
  });
  container.appendChild(nextItem);
}

// 加载页面
function loadPage(page) {
  // 子类实现
}

// 发送 API 请求
async function fetchAPI(url, options = {}) {
  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers
      }
    });
    
    if (!response.ok) {
      throw new Error('API 请求失败: ' + response.status);
    }
    
    return await response.json();
  } catch (error) {
    console.error('API 请求错误:', error);
    showErrorMessage('加载失败，请稍后重试');
    return null;
  }
}

// 创建不解析消息文本的提示框
function showAlertMessage(message, alertClass) {
  const alertElement = document.createElement('div');
  alertElement.className = `alert ${alertClass} alert-dismissible fade show`;
  alertElement.role = 'alert';
  alertElement.appendChild(document.createTextNode(String(message ?? '')));

  const closeButton = document.createElement('button');
  closeButton.type = 'button';
  closeButton.className = 'btn-close';
  closeButton.dataset.bsDismiss = 'alert';
  closeButton.setAttribute('aria-label', '关闭');
  alertElement.appendChild(closeButton);

  const container = document.querySelector('.container');
  if (container) {
    container.insertBefore(alertElement, container.firstChild);
    setTimeout(function() {
      alertElement.classList.remove('show');
      setTimeout(function() {
        alertElement.remove();
      }, 500);
    }, 3000);
  }
}

// 显示错误消息
function showErrorMessage(message) {
  showAlertMessage(message, 'alert-danger');
}

// 显示成功消息
function showSuccessMessage(message) {
  showAlertMessage(message, 'alert-success');
}

// 格式化日期
function formatDate(dateString) {
  if (typeof dateString !== 'string' || dateString.trim() === '') {
    return null;
  }

  try {
    let date;

    // 尝试解析 EXIF 格式日期 (YYYY:MM:DD HH:MM:SS)
    if (dateString.includes(':') && dateString.match(/^\d{4}:\d{2}:\d{2}/)) {
      const parts = dateString.split(' ');
      if (parts.length >= 2) {
        const dateParts = parts[0].split(':');
        const timeParts = parts[1].split(':');
        if (dateParts.length === 3 && timeParts.length >= 2) {
          const standardDate = `${dateParts[0]}-${dateParts[1]}-${dateParts[2]} ${timeParts[0]}:${timeParts[1]}`;
          date = new Date(standardDate);
        }
      }
    }

    if (!date || isNaN(date.getTime())) {
      date = new Date(dateString);
    }

    if (isNaN(date.getTime())) {
      console.error('Invalid date:', dateString);
      return null;
    }

    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    });
  } catch (error) {
    console.error('日期格式化错误:', error, dateString);
    return null;
  }
}

// 格式化时间
function formatTime(dateString) {
  const date = new Date(dateString);
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit'
  });
}

// 规范化照片编号，避免把任意文本写入链接路径
function normalizePhotoId(value) {
  const photoId = Number.parseInt(String(value), 10);
  return Number.isSafeInteger(photoId) && photoId > 0 ? photoId : null;
}

// 规范化评分，避免非法数值进入样式或无障碍属性
function normalizePhotoScore(value) {
  const score = Number(value);
  return Number.isFinite(score) ? Math.min(100, Math.max(0, score)) : 0;
}

// 只允许公开照片媒体接口的同源 URL
function getSafePhotoMediaUrl(value, expectedPath, fallbackPath) {
  if (!value) return fallbackPath;
  try {
    const url = new URL(String(value), window.location.origin);
    if (url.origin === window.location.origin && url.pathname === expectedPath) {
      return url.href;
    }
  } catch (error) {
    console.error('照片媒体 URL 无效:', error);
  }
  return fallbackPath;
}

// 创建安全的评分进度条
function createScoreBar(label, value, colorClass) {
  const score = normalizePhotoScore(value);
  const wrapper = document.createElement('div');
  wrapper.className = 'mb-2';
  wrapper.dataset.bsToggle = 'tooltip';
  wrapper.dataset.bsTitle = `${label}：${score}`;

  const progress = document.createElement('div');
  progress.className = 'progress';
  progress.style.height = '8px';

  const bar = document.createElement('div');
  bar.className = `progress-bar ${colorClass}`;
  bar.role = 'progressbar';
  bar.style.width = `${score}%`;
  bar.setAttribute('aria-valuenow', String(score));
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');
  progress.appendChild(bar);
  wrapper.appendChild(progress);
  return wrapper;
}

// 生成照片卡片，所有接口字段均通过 DOM 文本或属性接口写入
function generatePhotoCard(photo) {
  const cardContainer = document.createElement('div');
  cardContainer.className = 'photo-card-container col-xl-2 col-lg-3 col-md-4 col-sm-6';

  const card = document.createElement('div');
  card.className = 'photo-card card';

  const photoLink = document.createElement('a');
  photoLink.className = 'card-img-top';
  const photoId = normalizePhotoId(photo.id);
  photoLink.href = photoId === null ? '#' : `/photo/${encodeURIComponent(String(photoId))}`;

  const image = document.createElement('img');
  image.className = 'w-100';
  image.src = getSafePhotoMediaUrl(
    photo.thumbnail_url,
    '/api/photo/thumbnail',
    getStaticPath('images/placeholder.jpg')
  );
  image.alt = String(photo.title || '照片');
  photoLink.appendChild(image);
  card.appendChild(photoLink);

  const cardBody = document.createElement('div');
  cardBody.className = 'card-body';
  const caption = document.createElement('p');
  caption.className = 'card-text';
  caption.textContent = String(photo.side_caption || '');
  cardBody.appendChild(caption);

  const scoreContainer = document.createElement('div');
  scoreContainer.className = 'mt-3';
  scoreContainer.appendChild(createScoreBar('回忆度', photo.memory_score, 'bg-primary'));
  scoreContainer.appendChild(createScoreBar('美观度', photo.beauty_score, 'bg-success'));
  cardBody.appendChild(scoreContainer);
  card.appendChild(cardBody);

  const footer = document.createElement('div');
  footer.className = 'card-footer';
  const formattedDate = formatDate(photo.date_taken);
  if (formattedDate) {
    const date = document.createElement('small');
    date.className = 'text-muted';
    date.textContent = formattedDate;
    footer.appendChild(date);
  }
  if (photo.location) {
    const location = document.createElement('small');
    location.className = 'text-muted float-end';
    location.textContent = String(photo.location);
    footer.appendChild(location);
  }
  card.appendChild(footer);
  cardContainer.appendChild(card);
  return cardContainer;
}