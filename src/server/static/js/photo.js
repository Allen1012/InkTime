// 照片详情相关脚本

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
  console.log('照片详情页加载完成');
  // 初始化照片详情
  initPhotoDetail();
  
  // 初始化相关照片
  initRelatedPhotos();
});

// 初始化照片详情
async function initPhotoDetail() {
  console.log('初始化照片详情');
  showLoading();
  
  // 获取照片 ID
  const photoId = getPhotoIdFromUrl();
  console.log('照片 ID:', photoId);
  
  // 从真实 API 获取数据
  const photo = await fetchPhotoDetail(photoId);
  
  if (photo) {
    console.log('获取到的照片数据:', photo);
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
  console.log('开始渲染照片详情');
  
  // 渲染照片
  const mainPhoto = document.getElementById('main-photo');
  if (mainPhoto) {
    mainPhoto.src = photo.image_url;
    mainPhoto.alt = photo.title;
  }
  
  // 渲染 side_caption（一句话描述）
  const photoSideCaption = document.getElementById('photo-side-caption');
  if (photoSideCaption) {
    console.log('side_caption:', photo.side_caption);
    if (photo.side_caption) {
      photoSideCaption.textContent = photo.side_caption;
      photoSideCaption.style.display = 'block';
    } else {
      photoSideCaption.style.display = 'none';
    }
  }
  
  // 渲染元信息（日期、地点、相机、分辨率）
  const photoMeta = document.getElementById('photo-meta');
  if (photoMeta) {
    let metaInfo = [];
    
    // 优先使用 EXIF 拍摄时间，其次使用 date_taken
    const photoDate = photo.exif_data && photo.exif_data['拍摄时间'] 
      ? photo.exif_data['拍摄时间'] 
      : photo.date_taken;
    
    console.log('photoDate:', photoDate);
    if (photoDate) {
      metaInfo.push(`<span class="d-inline-flex align-items-center gap-1"><i class="fa fa-calendar"></i> ${formatDate(photoDate)}</span>`);
    }
    
    console.log('location:', photo.location);
    if (photo.location) {
      metaInfo.push(`<span class="d-inline-flex align-items-center gap-1"><i class="fa fa-map-marker"></i> ${photo.location}</span>`);
    }
    
    console.log('camera:', photo.camera);
    if (photo.camera && photo.camera !== '未知') {
      metaInfo.push(`<span class="d-inline-flex align-items-center gap-1"><i class="fa fa-camera"></i> ${photo.camera}</span>`);
    }
    
    console.log('resolution:', photo.resolution);
    if (photo.resolution && photo.resolution !== '未知') {
      metaInfo.push(`<span class="d-inline-flex align-items-center gap-1"><i class="fa fa-image"></i> ${photo.resolution}</span>`);
    }
    
    console.log('metaInfo:', metaInfo);
    if (metaInfo.length > 0) {
      photoMeta.innerHTML = metaInfo.join('');
      photoMeta.style.display = 'flex';
    } else {
      photoMeta.style.display = 'none';
    }
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

// 格式化日期
function formatDate(dateString) {
  if (!dateString || dateString.trim() === '') return '';
  
  try {
    let date;
    
    // 尝试解析EXIF格式日期 (YYYY:MM:DD HH:MM:SS)
    if (dateString.includes(':') && dateString.match(/^\d{4}:\d{2}:\d{2}/)) {
      const parts = dateString.split(' ');
      if (parts.length >= 2) {
        const dateParts = parts[0].split(':');
        const timeParts = parts[1].split(':');
        if (dateParts.length === 3 && timeParts.length >= 2) {
          // 构建标准日期字符串
          const standardDate = `${dateParts[0]}-${dateParts[1]}-${dateParts[2]} ${timeParts[0]}:${timeParts[1]}`;
          date = new Date(standardDate);
        }
      }
    }
    
    // 如果EXIF格式解析失败，尝试标准格式
    if (!date || isNaN(date.getTime())) {
      date = new Date(dateString);
    }
    
    // 检查日期是否有效
    if (isNaN(date.getTime())) {
      console.error('Invalid date:', dateString);
      return dateString; // 返回原始字符串
    }
    
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    });
  } catch (error) {
    console.error('日期格式化错误:', error, dateString);
    return dateString; // 返回原始字符串
  }
}

// 初始化相关照片
async function initRelatedPhotos() {
  // 获取当前照片 ID
  const photoId = getPhotoIdFromUrl();
  console.log('相关照片 - 照片 ID:', photoId);
  
  // 获取当前照片的类别
  const currentPhoto = await fetchPhotoDetail(photoId);
  console.log('相关照片 - 当前照片数据:', currentPhoto);
  
  if (currentPhoto && currentPhoto.category) {
    console.log('相关照片 - 类别:', currentPhoto.category);
    // 根据类别获取相关照片
    const relatedPhotos = await fetchRelatedPhotosByCategory(currentPhoto.category, photoId);
    console.log('相关照片 - 获取到的照片:', relatedPhotos);
    
    if (relatedPhotos) {
      renderRelatedPhotos(relatedPhotos);
    }
  } else {
    console.log('相关照片 - 无法获取类别');
  }
}

// 从真实 API 按类别获取相关照片
async function fetchRelatedPhotosByCategory(category, currentPhotoId) {
  try {
    console.log('[相关照片] 开始获取，类别:', category, '当前照片 ID:', currentPhotoId);
    
    // 构建 API URL - 使用照片 API 获取同类型的照片，按回忆度排序
    const url = new URL('/api/photos', window.location.origin);
    url.searchParams.append('filter', category);
    url.searchParams.append('page', '1');
    url.searchParams.append('limit', '7'); // 多获取一张，因为要过滤掉当前照片
    url.searchParams.append('sort', 'memory'); // 按回忆度降序排序
    
    console.log('[相关照片] API URL:', url.toString());
    
    // 发送请求
    const response = await fetch(url);
    const data = await response.json();
    
    console.log('[相关照片] API 响应:', data);
    
    if (data.status === 'ok') {
      // 过滤掉当前照片
      const relatedPhotos = data.data.items.filter(photo => photo.id !== currentPhotoId);
      console.log('[相关照片] 过滤后的照片:', relatedPhotos);
      return relatedPhotos;
    } else {
      console.error('[相关照片] 获取失败:', data.message);
      return [];
    }
  } catch (error) {
    console.error('[相关照片] 获取异常:', error);
    return [];
  }
}

// 渲染相关照片
function renderRelatedPhotos(photos) {
  const relatedPhotosContainer = document.getElementById('related-photos');
  if (!relatedPhotosContainer) return;
  
  relatedPhotosContainer.innerHTML = '';
  
  // 如果没有相关照片，显示提示
  if (photos.length === 0) {
    relatedPhotosContainer.innerHTML = '<p class="text-muted">暂无相关照片</p>';
    return;
  }
  
  photos.forEach(function(photo) {
    const col = document.createElement('div');
    col.className = 'col-6 mb-2';
    
    col.innerHTML = `
      <a href="/photo/${photo.id}" class="d-block">
        <img src="${photo.thumbnail_url}" alt="${photo.title || '照片'}" class="w-100 rounded">
      </a>
    `;
    
    relatedPhotosContainer.appendChild(col);
  });
}
