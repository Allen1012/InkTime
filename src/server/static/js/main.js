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
});

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

// 显示错误消息
function showErrorMessage(message) {
  const errorElement = document.createElement('div');
  errorElement.className = 'alert alert-danger alert-dismissible fade show';
  errorElement.role = 'alert';
  errorElement.innerHTML = message + '<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="关闭"></button>';
  
  const container = document.querySelector('.container');
  if (container) {
    container.insertBefore(errorElement, container.firstChild);
    
    // 3秒后自动关闭
    setTimeout(function() {
      errorElement.classList.remove('show');
      setTimeout(function() {
        errorElement.remove();
      }, 500);
    }, 3000);
  }
}

// 显示成功消息
function showSuccessMessage(message) {
  const successElement = document.createElement('div');
  successElement.className = 'alert alert-success alert-dismissible fade show';
  successElement.role = 'alert';
  successElement.innerHTML = message + '<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="关闭"></button>';
  
  const container = document.querySelector('.container');
  if (container) {
    container.insertBefore(successElement, container.firstChild);
    
    // 3秒后自动关闭
    setTimeout(function() {
      successElement.classList.remove('show');
      setTimeout(function() {
        successElement.remove();
      }, 500);
    }, 3000);
  }
}

// 格式化日期
function formatDate(dateString) {
  const date = new Date(dateString);
  return date.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric'
  });
}

// 格式化时间
function formatTime(dateString) {
  const date = new Date(dateString);
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit'
  });
}

// 生成照片卡片
function generatePhotoCard(photo) {
  const cardContainer = document.createElement('div');
  cardContainer.className = 'photo-card-container col-xl-2 col-lg-3 col-md-4 col-sm-6';
  
  // 构建卡片内容
  let cardContent = `
    <div class="photo-card card">
      <a href="/photo/${photo.id}" class="card-img-top">
        <img src="${photo.thumbnail_url || getStaticPath('images/placeholder.jpg')}" alt="${photo.title || '照片'}" class="w-100">
      </a>
      <div class="card-body">
        <p class="card-text">${photo.side_caption || ''}</p>
        
        <!-- 评分条形图 -->
        <div class="mt-3">
          <div class="mb-2">
            <div class="d-flex justify-content-between">
              <small>回忆度</small>
            </div>
            <div class="progress" style="height: 6px;">
              <div class="progress-bar bg-primary" role="progressbar" style="width: ${photo.memory_score}%;" aria-valuenow="${photo.memory_score}" aria-valuemin="0" aria-valuemax="100"></div>
            </div>
          </div>
          <div class="mb-2">
            <div class="d-flex justify-content-between">
              <small>美观度</small>
            </div>
            <div class="progress" style="height: 6px;">
              <div class="progress-bar bg-success" role="progressbar" style="width: ${photo.beauty_score}%;" aria-valuenow="${photo.beauty_score}" aria-valuemin="0" aria-valuemax="100"></div>
            </div>
          </div>
        </div>
      </div>
      <div class="card-footer">
  `;
  
  // 只在有拍摄时间时展示
  if (photo.date_taken) {
    cardContent += `
        <small class="text-muted">${formatDate(photo.date_taken)}</small>
    `;
  }
  
  // 只在有位置时展示
  if (photo.location) {
    cardContent += `
        <small class="text-muted float-end">${photo.location}</small>
    `;
  }
  
  cardContent += `
      </div>
    </div>
  `;
  
  cardContainer.innerHTML = cardContent;
  
  return cardContainer;
}