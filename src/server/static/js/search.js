// 搜索相关脚本

// 全局变量
let currentPage = 1;
let currentQuery = '';
let totalPages = 1;

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化搜索页面
  initSearchPage();
});

// 初始化搜索页面
function initSearchPage() {
  // 获取搜索关键词
  currentQuery = getSearchQueryFromUrl();
  
  // 加载搜索结果
  loadSearchResults(currentPage, currentQuery);
}

// 从 URL 获取搜索关键词
function getSearchQueryFromUrl() {
  const urlParams = new URLSearchParams(window.location.search);
  return urlParams.get('q') || '';
}

// 加载搜索结果
async function loadSearchResults(page, query) {
  showLoading();
  
  // 模拟 API 请求
  const results = await mockFetchSearchResults(page, query);
  
  if (results) {
    renderSearchResults(results);
    
    // 更新分页
    totalPages = Math.ceil(results.total / 12); // 假设每页 12 张照片
    generatePagination(totalPages, page, 'pagination');
  }
  
  hideLoading();
}

// 模拟获取搜索结果
async function mockFetchSearchResults(page, query) {
  // 模拟延迟
  await new Promise(resolve => setTimeout(resolve, 500));
  
  // 模拟搜索结果数据
  const photos = [];
  const total = 20; // 总结果数
  
  // 生成模拟数据
  for (let i = (page - 1) * 12 + 1; i <= Math.min(page * 12, total); i++) {
    photos.push({
      id: i,
      title: `搜索结果 ${i} - ${query}`,
      description: `这是关于 "${query}" 的搜索结果 ${i}。`,
      date_taken: new Date(Date.now() - Math.random() * 365 * 24 * 60 * 60 * 1000).toISOString(),
      location: ['北京', '上海', '广州', '深圳', '杭州'][Math.floor(Math.random() * 5)],
      thumbnail_url: `https://picsum.photos/300/200?random=${i}`,
      category: ['person', 'landscape', 'food', 'pet', 'travel'][Math.floor(Math.random() * 5)],
      memory_score: Math.floor(Math.random() * 101),
      beauty_score: Math.floor(Math.random() * 101)
    });
  }
  
  return {
    items: photos,
    total: total,
    query: query
  };
}

// 渲染搜索结果
function renderSearchResults(results) {
  const searchResults = document.getElementById('search-results');
  if (!searchResults) return;
  
  // 清空内容
  searchResults.innerHTML = '';
  
  // 渲染照片卡片
  results.items.forEach(function(photo) {
    const card = generatePhotoCard(photo);
    searchResults.appendChild(card);
  });
  
  // 调整网格列数
  adjustPhotoGridColumns();
  
  // 更新搜索结果标题
  const resultTitle = document.querySelector('h2');
  if (resultTitle) {
    resultTitle.textContent = `搜索结果: ${results.query}`;
  }
}

// 加载页面（覆盖父类方法）
function loadPage(page) {
  currentPage = page;
  loadSearchResults(currentPage, currentQuery);
}