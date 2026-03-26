// 相册相关脚本

// 全局变量
let currentPage = 1;
let currentFilter = 'all';
let currentSort = 'latest';
let totalPages = 1;

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化相册
  initGallery();
  
  // 加载分类标签
  loadCategoryFilters();
  
  // 绑定筛选和排序事件
  bindFilterEvents();
  bindSortEvents();
  
  // 初始化无限滚动
  initInfiniteScroll();
});

// 加载分类标签
async function loadCategoryFilters() {
  try {
    const response = await fetch('/api/category/stats');
    const data = await response.json();
    
    if (data.status === 'ok') {
      const categories = data.data.categories;
      const filterContainer = document.getElementById('category-filters');
      
      if (filterContainer) {
        // 添加分类按钮
        categories.forEach(cat => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'btn btn-outline-primary category-filter-btn';
          btn.dataset.filter = cat.id;
          btn.textContent = `${cat.name} (${cat.count})`;
          filterContainer.appendChild(btn);
        });
        
        // 重新绑定筛选事件
        bindFilterEvents();
      }
    }
  } catch (error) {
    console.error('加载分类标签失败:', error);
  }
}

// 初始化相册
function initGallery() {
  // 加载第一页照片
  loadPhotos(currentPage, currentFilter, currentSort);
}

// 加载照片
async function loadPhotos(page, filter, sort) {
  showLoading();
  
  // 从真实 API 获取数据
  const photos = await fetchPhotos(page, filter, sort);
  
  if (photos) {
    renderPhotos(photos);
    
    // 更新分页
    totalPages = Math.ceil(photos.total / 12); // 假设每页 12 张照片
    generatePagination(totalPages, page, 'pagination');
  }
  
  hideLoading();
}

// 从真实 API 获取照片数据
async function fetchPhotos(page, filter, sort) {
  try {
    // 构建 API URL
    const url = new URL('/api/photos', window.location.origin);
    url.searchParams.append('page', page);
    url.searchParams.append('filter', filter);
    url.searchParams.append('sort', sort);
    url.searchParams.append('limit', 12);
    
    // 发送请求
    const response = await fetch(url);
    const data = await response.json();
    
    if (data.status === 'ok') {
      return {
        items: data.data.items,
        total: data.data.total
      };
    } else {
      console.error('API 请求失败:', data.message);
      return null;
    }
  } catch (error) {
    console.error('获取照片数据失败:', error);
    return null;
  }
}

// 渲染照片
function renderPhotos(photos) {
  const photoGrid = document.getElementById('photo-grid');
  if (!photoGrid) return;
  
  // 清空网格
  photoGrid.innerHTML = '';
  
  // 渲染照片卡片
  photos.items.forEach(function(photo) {
    const card = generatePhotoCard(photo);
    photoGrid.appendChild(card);
  });
  
  // 调整网格列数
  adjustPhotoGridColumns();
}

// 绑定筛选事件
function bindFilterEvents() {
  const filterButtons = document.querySelectorAll('[data-filter]');
  filterButtons.forEach(function(button) {
    button.addEventListener('click', function() {
      // 更新活跃状态
      filterButtons.forEach(function(btn) {
        btn.classList.remove('active');
      });
      this.classList.add('active');
      
      // 更新筛选条件
      currentFilter = this.dataset.filter;
      currentPage = 1;
      
      // 重新加载照片
      loadPhotos(currentPage, currentFilter, currentSort);
    });
  });
}

// 绑定排序事件
function bindSortEvents() {
  const sortButtons = document.querySelectorAll('[data-sort]');
  sortButtons.forEach(function(button) {
    button.addEventListener('click', function() {
      // 更新活跃状态
      sortButtons.forEach(function(btn) {
        btn.classList.remove('active');
      });
      this.classList.add('active');
      
      // 更新排序条件
      currentSort = this.dataset.sort;
      currentPage = 1;
      
      // 重新加载照片
      loadPhotos(currentPage, currentFilter, currentSort);
    });
  });
}

// 初始化无限滚动
function initInfiniteScroll() {
  // 检查浏览器是否支持 Intersection Observer API
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(function(entries) {
      const entry = entries[0];
      if (entry.isIntersecting && currentPage < totalPages) {
        currentPage++;
        loadMorePhotos();
      }
    }, {
      rootMargin: '0px 0px 200px 0px'
    });
    
    const pagination = document.getElementById('pagination');
    if (pagination) {
      observer.observe(pagination);
    }
  }
}

// 加载更多照片
async function loadMorePhotos() {
  showLoading();
  
  // 从真实 API 获取数据
  const photos = await fetchPhotos(currentPage, currentFilter, currentSort);
  
  if (photos) {
    appendPhotos(photos);
    
    // 更新分页
    totalPages = Math.ceil(photos.total / 12);
    generatePagination(totalPages, currentPage, 'pagination');
  }
  
  hideLoading();
}

// 追加照片
function appendPhotos(photos) {
  const photoGrid = document.getElementById('photo-grid');
  if (!photoGrid) return;
  
  // 渲染照片卡片
  photos.items.forEach(function(photo) {
    const card = generatePhotoCard(photo);
    photoGrid.appendChild(card);
  });
  
  // 调整网格列数
  adjustPhotoGridColumns();
}

// 加载页面（覆盖父类方法）
function loadPage(page) {
  currentPage = page;
  loadPhotos(currentPage, currentFilter, currentSort);
}

// 照片预览模态框
function initPhotoModal() {
  const modal = document.getElementById('photoModal');
  if (!modal) return;
  
  const modalBody = modal.querySelector('.modal-body');
  const modalTitle = modal.querySelector('.modal-title');
  
  // 绑定照片点击事件
  document.addEventListener('click', function(e) {
    if (e.target.closest('.photo-card img')) {
      const img = e.target.closest('.photo-card img');
      const title = img.closest('.photo-card').querySelector('.card-title').textContent;
      
      // 设置模态框内容
      modalTitle.textContent = title;
      modalBody.innerHTML = `<img src="${img.src}" alt="${title}" class="w-100">`;
      
      // 显示模态框
      const bootstrapModal = new bootstrap.Modal(modal);
      bootstrapModal.show();
    }
  });
}

// 初始化照片预览模态框
initPhotoModal();