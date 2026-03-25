// 分类相关脚本

// 全局变量
let currentPage = 1;
let currentCategory = 'all';
let totalPages = 1;
let categoryStats = {};

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化分类页面
  initCategoryPage();
});

// 初始化分类页面
async function initCategoryPage() {
  // 加载分类统计
  await loadCategoryStats();
  
  // 加载默认分类的照片
  loadCategoryPhotos(currentPage, currentCategory);
  
  // 绑定卡片点击事件
  bindCategoryCardEvents();
}

// 加载分类统计
async function loadCategoryStats() {
  try {
    const response = await fetch('/api/category/stats');
    const data = await response.json();
    
    if (data.status === 'ok') {
      categoryStats = data.data;
      renderCategoryStats();
    }
  } catch (error) {
    console.error('加载分类统计失败:', error);
  }
}

// 渲染分类统计
function renderCategoryStats() {
  const statsContainer = document.getElementById('category-stats');
  if (!statsContainer) return;
  
  const total = categoryStats.total || 0;
  const categories = categoryStats.categories || [];
  
  let statsHTML = '';
  
  // 添加"全部照片"卡片
  statsHTML += `
    <div class="col-md-4 col-sm-6 mb-3">
      <div class="card h-100 category-card" data-category="all" style="cursor: pointer;">
        <div class="card-body">
          <h5 class="card-title">全部照片</h5>
          <p class="card-text">
            <span class="display-4">${total}</span>
            <span class="text-muted">张</span>
          </p>
          <div class="progress" style="height: 10px;">
            <div class="progress-bar bg-primary" role="progressbar" 
                 style="width: 100%" 
                 aria-valuenow="100" 
                 aria-valuemin="0" 
                 aria-valuemax="100"></div>
          </div>
          <small class="text-muted">100%</small>
        </div>
      </div>
    </div>
  `;
  
  // 添加各个分类卡片
  categories.forEach(cat => {
    const count = cat.count;
    const percentage = total > 0 ? ((count / total) * 100).toFixed(1) : 0;
    
    statsHTML += `
      <div class="col-md-4 col-sm-6 mb-3">
        <div class="card h-100 category-card" data-category="${cat.id}" style="cursor: pointer;">
          <div class="card-body">
            <h5 class="card-title">${cat.name}</h5>
            <p class="card-text">
              <span class="display-4">${count}</span>
              <span class="text-muted">张</span>
            </p>
            <div class="progress" style="height: 10px;">
              <div class="progress-bar" role="progressbar" 
                   style="width: ${percentage}%" 
                   aria-valuenow="${percentage}" 
                   aria-valuemin="0" 
                   aria-valuemax="100"></div>
            </div>
            <small class="text-muted">${percentage}%</small>
          </div>
        </div>
      </div>
    `;
  });
  
  statsContainer.innerHTML = statsHTML;
}

// 加载分类照片
async function loadCategoryPhotos(page, category) {
  showLoading();
  
  // 从真实 API 获取数据
  const photos = await fetchCategoryPhotos(page, category);
  
  if (photos) {
    renderCategoryPhotos(photos);
    
    // 更新分页
    totalPages = Math.ceil(photos.total / 12);
    generatePagination(totalPages, page, 'pagination');
  }
  
  hideLoading();
}

// 从真实 API 获取分类照片
async function fetchCategoryPhotos(page, category) {
  try {
    const url = new URL('/api/category/photos', window.location.origin);
    url.searchParams.append('category', category);
    url.searchParams.append('page', page);
    url.searchParams.append('limit', 12);
    
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
    console.error('获取分类照片失败:', error);
    return null;
  }
}

// 渲染分类照片
function renderCategoryPhotos(photos) {
  const categoryContent = document.getElementById('category-content');
  if (!categoryContent) return;
  
  // 清空内容
  categoryContent.innerHTML = '';
  
  // 渲染照片卡片
  photos.items.forEach(function(photo) {
    const card = generatePhotoCard(photo);
    categoryContent.appendChild(card);
  });
  
  // 调整网格列数
  adjustPhotoGridColumns();
}

// 绑定分类卡片事件
function bindCategoryCardEvents() {
  document.addEventListener('click', function(e) {
    const card = e.target.closest('.category-card');
    if (card) {
      // 更新活跃状态
      document.querySelectorAll('.category-card').forEach(c => {
        c.classList.remove('active');
      });
      card.classList.add('active');
      
      // 更新分类
      currentCategory = card.dataset.category;
      currentPage = 1;
      
      // 重新加载照片
      loadCategoryPhotos(currentPage, currentCategory);
    }
  });
}

// 加载页面（覆盖父类方法）
function loadPage(page) {
  currentPage = page;
  loadCategoryPhotos(currentPage, currentCategory);
}
