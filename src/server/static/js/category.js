// 分类相关脚本

// 全局变量
let currentPage = 1;
let currentCategory = 'all';
let totalPages = 1;

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化分类页面
  initCategoryPage();
  
  // 绑定分类事件
  bindCategoryEvents();
});

// 初始化分类页面
function initCategoryPage() {
  // 加载默认分类的照片
  loadCategoryPhotos(currentPage, currentCategory);
}

// 加载分类照片
async function loadCategoryPhotos(page, category) {
  showLoading();
  
  // 模拟 API 请求
  const photos = await mockFetchCategoryPhotos(page, category);
  
  if (photos) {
    renderCategoryPhotos(photos);
    
    // 更新分页
    totalPages = Math.ceil(photos.total / 12); // 假设每页 12 张照片
    generatePagination(totalPages, page, 'pagination');
  }
  
  hideLoading();
}

// 模拟获取分类照片数据
async function mockFetchCategoryPhotos(page, category) {
  // 模拟延迟
  await new Promise(resolve => setTimeout(resolve, 500));
  
  // 模拟照片数据
  const photos = [];
  const total = 30; // 总照片数
  
  // 生成模拟数据
  for (let i = (page - 1) * 12 + 1; i <= Math.min(page * 12, total); i++) {
    photos.push({
      id: i,
      title: `${getCategoryName(category)} 照片 ${i}`,
      description: `这是一张 ${getCategoryName(category)} 类别的模拟照片 ${i}。`,
      date_taken: new Date(Date.now() - Math.random() * 365 * 24 * 60 * 60 * 1000).toISOString(),
      location: ['北京', '上海', '广州', '深圳', '杭州'][Math.floor(Math.random() * 5)],
      thumbnail_url: `https://picsum.photos/300/200?random=${i}`,
      category: category === 'all' ? ['person', 'landscape', 'food', 'pet', 'travel'][Math.floor(Math.random() * 5)] : category,
      memory_score: Math.floor(Math.random() * 101),
      beauty_score: Math.floor(Math.random() * 101)
    });
  }
  
  return {
    items: photos,
    total: total
  };
}

// 获取分类名称
function getCategoryName(category) {
  const categoryMap = {
    'all': '全部',
    'person': '人物',
    'landscape': '风景',
    'food': '美食',
    'pet': '宠物',
    'travel': '旅行',
    'other': '其他'
  };
  return categoryMap[category] || '全部';
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

// 绑定分类事件
function bindCategoryEvents() {
  const categoryButtons = document.querySelectorAll('[data-category]');
  categoryButtons.forEach(function(button) {
    button.addEventListener('click', function() {
      // 更新活跃状态
      categoryButtons.forEach(function(btn) {
        btn.classList.remove('active');
      });
      this.classList.add('active');
      
      // 更新分类
      currentCategory = this.dataset.category;
      currentPage = 1;
      
      // 重新加载照片
      loadCategoryPhotos(currentPage, currentCategory);
    });
  });
}

// 加载页面（覆盖父类方法）
function loadPage(page) {
  currentPage = page;
  loadCategoryPhotos(currentPage, currentCategory);
}