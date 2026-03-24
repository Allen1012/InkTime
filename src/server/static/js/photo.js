// 照片详情相关脚本

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  // 初始化照片详情
  initPhotoDetail();
  
  // 初始化相关照片
  initRelatedPhotos();
});

// 初始化照片详情
async function initPhotoDetail() {
  showLoading();
  
  // 获取照片 ID
  const photoId = getPhotoIdFromUrl();
  
  // 从真实 API 获取数据
  const photo = await fetchPhotoDetail(photoId);
  
  if (photo) {
    renderPhotoDetail(photo);
  }
  
  hideLoading();
}

// 从 URL 获取照片 ID
function getPhotoIdFromUrl() {
  const pathParts = window.location.pathname.split('/');
  return pathParts[pathParts.length - 1] || '1';
}

// 从真实 API 获取照片详情
async function fetchPhotoDetail(photoId) {
  try {
    // 构建 API URL
    const url = new URL(`/api/photo/${photoId}`, window.location.origin);
    
    // 发送请求
    const response = await fetch(url);
    const data = await response.json();
    
    if (data.status === 'ok') {
      return data.data;
    } else {
      console.error('API 请求失败:', data.message);
      showErrorMessage('获取照片详情失败');
      return null;
    }
  } catch (error) {
    console.error('获取照片详情失败:', error);
    showErrorMessage('加载失败，请稍后重试');
    return null;
  }
}

// 渲染照片详情
function renderPhotoDetail(photo) {
  // 渲染照片
  const mainPhoto = document.getElementById('main-photo');
  if (mainPhoto) {
    mainPhoto.src = photo.image_url;
    mainPhoto.alt = photo.title;
  }
  
  // 渲染标题
  const photoTitle = document.getElementById('photo-title');
  if (photoTitle) {
    photoTitle.textContent = photo.title;
  }
  
  // 渲染描述
  const photoDescription = document.getElementById('photo-description');
  if (photoDescription) {
    photoDescription.textContent = photo.description;
  }
  
  // 渲染日期
  const photoDate = document.getElementById('photo-date');
  if (photoDate) {
    photoDate.textContent = formatDate(photo.date_taken);
  }
  
  // 渲染位置
  const photoLocation = document.getElementById('photo-location');
  if (photoLocation) {
    photoLocation.textContent = photo.location;
  }
  
  // 渲染相机
  const photoCamera = document.getElementById('photo-camera');
  if (photoCamera) {
    photoCamera.textContent = photo.camera;
  }
  
  // 渲染分辨率
  const photoResolution = document.getElementById('photo-resolution');
  if (photoResolution) {
    photoResolution.textContent = photo.resolution;
  }
  
  // 渲染回忆度评分
  const memoryScore = document.getElementById('memory-score');
  if (memoryScore) {
    memoryScore.textContent = photo.memory_score + '%';
  }
  
  // 渲染回忆度进度条
  const memoryProgress = document.getElementById('memory-progress');
  if (memoryProgress) {
    memoryProgress.style.width = photo.memory_score + '%';
    memoryProgress.setAttribute('aria-valuenow', photo.memory_score);
  }
  
  // 渲染美观度评分
  const beautyScore = document.getElementById('beauty-score');
  if (beautyScore) {
    beautyScore.textContent = photo.beauty_score + '%';
  }
  
  // 渲染美观度进度条
  const beautyProgress = document.getElementById('beauty-progress');
  if (beautyProgress) {
    beautyProgress.style.width = photo.beauty_score + '%';
    beautyProgress.setAttribute('aria-valuenow', photo.beauty_score);
  }
  
  // 渲染评分原因
  const scoreReason = document.getElementById('score-reason');
  if (scoreReason) {
    scoreReason.textContent = photo.score_reason;
  }
  
  // 渲染 EXIF 数据
  const exifData = document.getElementById('exif-data');
  if (exifData) {
    exifData.innerHTML = '';
    for (const [key, value] of Object.entries(photo.exif_data)) {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td class="font-weight-bold">${key}</td>
        <td>${value}</td>
      `;
      exifData.appendChild(row);
    }
  }
}

// 初始化相关照片
async function initRelatedPhotos() {
  // 获取照片 ID
  const photoId = getPhotoIdFromUrl();
  
  // 模拟 API 请求
  const relatedPhotos = await mockFetchRelatedPhotos(photoId);
  
  if (relatedPhotos) {
    renderRelatedPhotos(relatedPhotos);
  }
}

// 模拟获取相关照片
async function mockFetchRelatedPhotos(photoId) {
  // 模拟延迟
  await new Promise(resolve => setTimeout(resolve, 300));
  
  // 模拟相关照片数据
  const photos = [];
  for (let i = 1; i <= 6; i++) {
    photos.push({
      id: i + parseInt(photoId),
      title: `相关照片 ${i}`,
      thumbnail_url: `https://picsum.photos/200/150?random=${i + parseInt(photoId)}`,
      date_taken: new Date(Date.now() - Math.random() * 365 * 24 * 60 * 60 * 1000).toISOString()
    });
  }
  
  return photos;
}

// 渲染相关照片
function renderRelatedPhotos(photos) {
  const relatedPhotosContainer = document.getElementById('related-photos');
  if (!relatedPhotosContainer) return;
  
  relatedPhotosContainer.innerHTML = '';
  
  photos.forEach(function(photo) {
    const col = document.createElement('div');
    col.className = 'col-4 mb-2';
    
    col.innerHTML = `
      <a href="/photo/${photo.id}" class="d-block">
        <img src="${photo.thumbnail_url}" alt="${photo.title}" class="w-100 rounded">
      </a>
    `;
    
    relatedPhotosContainer.appendChild(col);
  });
}