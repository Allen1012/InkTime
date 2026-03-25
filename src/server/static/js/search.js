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
  
  // 显示搜索关键词
  const searchQueryElement = document.getElementById('search-query');
  if (searchQueryElement) {
    searchQueryElement.textContent = currentQuery;
  }
  
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
  
  // 从真实 API 获取数据
  const results = await fetchSearchResults(page, query);
  
  if (results) {
    renderSearchResults(results);
    
    // 更新分页
    totalPages = Math.ceil(results.total / 12);
    generatePagination(totalPages, page, 'pagination');
  }
  
  hideLoading();
}

// 从真实 API 获取搜索结果
async function fetchSearchResults(page, query) {
  try {
    // 构建 API URL
    const url = new URL('/api/search', window.location.origin);
    url.searchParams.append('q', query);
    url.searchParams.append('page', page);
    url.searchParams.append('limit', 12);
    
    // 发送请求
    const response = await fetch(url);
    const data = await response.json();
    
    if (data.status === 'ok') {
      return {
        items: data.data.items,
        total: data.data.total,
        query: query
      };
    } else {
      console.error('API 请求失败:', data.message);
      return null;
    }
  } catch (error) {
    console.error('获取搜索结果失败:', error);
    return null;
  }
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