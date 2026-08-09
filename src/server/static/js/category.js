// 分类相关脚本

// 全局变量
let currentPage = 1;
let currentCategory = 'all';
let totalPages = 1;
let categoryStats = {};

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  initCategoryPage();
});

// 初始化分类页面
async function initCategoryPage() {
  await loadCategoryStats();
  loadCategoryPhotos(currentPage, currentCategory);
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

// 把外部计数规范化为非负数
function normalizeCategoryCount(value) {
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, count) : 0;
}

// 创建分类统计卡片，分类字段只通过文本和 dataset 写入
function createCategoryCard(categoryId, categoryName, count, percentage, primary) {
  const column = document.createElement('div');
  column.className = 'col-md-4 col-sm-6 mb-3';

  const card = document.createElement('div');
  card.className = 'card h-100 category-card';
  card.dataset.category = String(categoryId);
  card.style.cursor = 'pointer';

  const body = document.createElement('div');
  body.className = 'card-body';

  const title = document.createElement('h5');
  title.className = 'card-title';
  title.textContent = String(categoryName);
  body.appendChild(title);

  const countText = document.createElement('p');
  countText.className = 'card-text';
  const countValue = document.createElement('span');
  countValue.className = 'display-4';
  countValue.textContent = String(count);
  const countUnit = document.createElement('span');
  countUnit.className = 'text-muted';
  countUnit.textContent = '张';
  countText.append(countValue, countUnit);
  body.appendChild(countText);

  const progress = document.createElement('div');
  progress.className = 'progress';
  progress.style.height = '10px';
  const progressBar = document.createElement('div');
  progressBar.className = primary ? 'progress-bar bg-primary' : 'progress-bar';
  progressBar.role = 'progressbar';
  progressBar.style.width = `${percentage}%`;
  progressBar.setAttribute('aria-valuenow', String(percentage));
  progressBar.setAttribute('aria-valuemin', '0');
  progressBar.setAttribute('aria-valuemax', '100');
  progress.appendChild(progressBar);
  body.appendChild(progress);

  const percentageText = document.createElement('small');
  percentageText.className = 'text-muted';
  percentageText.textContent = `${percentage}%`;
  body.appendChild(percentageText);
  card.appendChild(body);
  column.appendChild(card);
  return column;
}

// 渲染分类统计
function renderCategoryStats() {
  const statsContainer = document.getElementById('category-stats');
  if (!statsContainer) return;

  const total = normalizeCategoryCount(categoryStats.total);
  const categories = Array.isArray(categoryStats.categories) ? categoryStats.categories : [];
  const fragment = document.createDocumentFragment();
  fragment.appendChild(createCategoryCard('all', '全部照片', total, 100, true));

  categories.forEach(function(category) {
    const count = normalizeCategoryCount(category.count);
    const percentage = total > 0 ? Math.min(100, (count / total) * 100).toFixed(1) : '0.0';
    fragment.appendChild(
      createCategoryCard(category.id, category.name, count, percentage, false)
    );
  });

  statsContainer.replaceChildren(fragment);
}

// 加载分类照片
async function loadCategoryPhotos(page, category) {
  showLoading();
  const photos = await fetchCategoryPhotos(page, category);

  if (photos) {
    renderCategoryPhotos(photos);
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
    }
    console.error('API 请求失败:', data.message);
    return null;
  } catch (error) {
    console.error('获取分类照片失败:', error);
    return null;
  }
}

// 渲染分类照片
function renderCategoryPhotos(photos) {
  const categoryContent = document.getElementById('category-content');
  if (!categoryContent) return;

  categoryContent.replaceChildren();
  photos.items.forEach(function(photo) {
    categoryContent.appendChild(generatePhotoCard(photo));
  });
  adjustPhotoGridColumns();
}

// 绑定分类卡片事件
function bindCategoryCardEvents() {
  document.addEventListener('click', function(event) {
    const card = event.target.closest('.category-card');
    if (card) {
      document.querySelectorAll('.category-card').forEach(function(item) {
        item.classList.remove('active');
      });
      card.classList.add('active');
      currentCategory = card.dataset.category;
      currentPage = 1;
      loadCategoryPhotos(currentPage, currentCategory);
    }
  });
}

// 加载页面（覆盖父类方法）
function loadPage(page) {
  currentPage = page;
  loadCategoryPhotos(currentPage, currentCategory);
}
