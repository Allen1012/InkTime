// 照片详情相关脚本

document.addEventListener('DOMContentLoaded', function() {
  console.log('照片详情页加载完成');
  initPhotoDetail();
  initRelatedPhotos();
});

// 初始化照片详情
async function initPhotoDetail() {
  console.log('初始化照片详情');
  showLoading();
  const photoId = getPhotoIdFromUrl();
  console.log('照片 ID:', photoId);
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
    const normalizedId = normalizePhotoId(photoId);
    if (normalizedId === null) {
      throw new Error('照片 ID 无效');
    }
    const url = new URL(`/api/photo/${encodeURIComponent(String(normalizedId))}`, window.location.origin);
    const response = await fetch(url);
    const data = await response.json();

    if (data.status === 'ok') {
      return data.data;
    }
    console.error('API 请求失败:', data.message);
    showErrorMessage('获取照片详情失败');
    return null;
  } catch (error) {
    console.error('获取照片详情失败:', error);
    showErrorMessage('加载失败，请稍后重试');
    return null;
  }
}

// 创建照片元信息项，外部字段只写入文本节点
function createPhotoMetaItem(iconClass, value) {
  const item = document.createElement('span');
  item.className = 'd-inline-flex align-items-center gap-1';
  const icon = document.createElement('i');
  icon.className = `fa ${iconClass}`;
  icon.setAttribute('aria-hidden', 'true');
  item.appendChild(icon);
  item.appendChild(document.createTextNode(` ${String(value)}`));
  return item;
}

// 渲染照片详情
function renderPhotoDetail(photo) {
  console.log('开始渲染照片详情');

  const mainPhoto = document.getElementById('main-photo');
  if (mainPhoto) {
    mainPhoto.src = getSafePhotoMediaUrl(
      photo.image_url,
      '/api/photo/full',
      getStaticPath('images/placeholder.jpg')
    );
    mainPhoto.alt = String(photo.title || '照片');
  }

  const photoSideCaption = document.getElementById('photo-side-caption');
  if (photoSideCaption) {
    if (photo.side_caption) {
      photoSideCaption.textContent = String(photo.side_caption);
      photoSideCaption.style.display = 'block';
    } else {
      photoSideCaption.replaceChildren();
      photoSideCaption.style.display = 'none';
    }
  }

  const photoMeta = document.getElementById('photo-meta');
  if (photoMeta) {
    const exifData = photo.exif_data && typeof photo.exif_data === 'object' ? photo.exif_data : {};
    const photoDate = exifData['拍摄时间'] || photo.date_taken;
    const metaItems = [];
    const formattedDate = formatDate(photoDate);
    if (formattedDate) metaItems.push(createPhotoMetaItem('fa-calendar', formattedDate));
    if (photo.location) metaItems.push(createPhotoMetaItem('fa-map-marker', photo.location));
    if (photo.camera && photo.camera !== '未知') metaItems.push(createPhotoMetaItem('fa-camera', photo.camera));
    if (photo.resolution && photo.resolution !== '未知') metaItems.push(createPhotoMetaItem('fa-image', photo.resolution));
    photoMeta.replaceChildren(...metaItems);
    photoMeta.style.display = metaItems.length > 0 ? 'flex' : 'none';
  }

  const memoryValue = normalizePhotoScore(photo.memory_score);
  const memoryScore = document.getElementById('memory-score');
  if (memoryScore) memoryScore.textContent = `${memoryValue}%`;
  const memoryProgress = document.getElementById('memory-progress');
  if (memoryProgress) {
    memoryProgress.style.width = `${memoryValue}%`;
    memoryProgress.setAttribute('aria-valuenow', String(memoryValue));
  }

  const beautyValue = normalizePhotoScore(photo.beauty_score);
  const beautyScore = document.getElementById('beauty-score');
  if (beautyScore) beautyScore.textContent = `${beautyValue}%`;
  const beautyProgress = document.getElementById('beauty-progress');
  if (beautyProgress) {
    beautyProgress.style.width = `${beautyValue}%`;
    beautyProgress.setAttribute('aria-valuenow', String(beautyValue));
  }

  const scoreReason = document.getElementById('score-reason');
  if (scoreReason) scoreReason.textContent = String(photo.score_reason || '');

  const exifTable = document.getElementById('exif-data');
  if (exifTable) {
    exifTable.replaceChildren();
    const exifData = photo.exif_data && typeof photo.exif_data === 'object' ? photo.exif_data : {};
    for (const [key, value] of Object.entries(exifData)) {
      const row = document.createElement('tr');
      const keyCell = document.createElement('td');
      keyCell.className = 'font-weight-bold';
      keyCell.textContent = String(key);
      const valueCell = document.createElement('td');
      valueCell.textContent = String(value ?? '');
      row.append(keyCell, valueCell);
      exifTable.appendChild(row);
    }
  }
}

// 格式化日期
function formatDate(dateString) {
  if (typeof dateString !== 'string' || dateString.trim() === '') return '';

  try {
    let date;
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
    if (!date || isNaN(date.getTime())) date = new Date(dateString);
    if (isNaN(date.getTime())) {
      console.error('Invalid date:', dateString);
      return dateString;
    }
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    });
  } catch (error) {
    console.error('日期格式化错误:', error, dateString);
    return dateString;
  }
}

// 初始化相关照片
async function initRelatedPhotos() {
  const photoId = getPhotoIdFromUrl();
  const currentPhoto = await fetchPhotoDetail(photoId);
  if (currentPhoto && currentPhoto.category) {
    const relatedPhotos = await fetchRelatedPhotosByCategory(currentPhoto.category, photoId);
    renderRelatedPhotos(relatedPhotos);
  }
}

// 从真实 API 按类别获取相关照片
async function fetchRelatedPhotosByCategory(category, currentPhotoId) {
  try {
    const url = new URL('/api/photos', window.location.origin);
    url.searchParams.append('filter', category);
    url.searchParams.append('page', '1');
    url.searchParams.append('limit', '7');
    url.searchParams.append('sort', 'memory');
    const response = await fetch(url);
    const data = await response.json();

    if (data.status === 'ok') {
      return data.data.items.filter(function(photo) {
        return String(photo.id) !== String(currentPhotoId);
      });
    }
    console.error('[相关照片] 获取失败:', data.message);
    return [];
  } catch (error) {
    console.error('[相关照片] 获取异常:', error);
    return [];
  }
}

// 渲染相关照片，链接与媒体 URL 均使用属性接口
function renderRelatedPhotos(photos) {
  const relatedPhotosContainer = document.getElementById('related-photos');
  if (!relatedPhotosContainer) return;

  relatedPhotosContainer.replaceChildren();
  if (photos.length === 0) {
    const emptyMessage = document.createElement('p');
    emptyMessage.className = 'text-muted';
    emptyMessage.textContent = '暂无相关照片';
    relatedPhotosContainer.appendChild(emptyMessage);
    return;
  }

  photos.forEach(function(photo) {
    const col = document.createElement('div');
    col.className = 'col-6 mb-2';
    const link = document.createElement('a');
    link.className = 'd-block';
    const photoId = normalizePhotoId(photo.id);
    link.href = photoId === null ? '#' : `/photo/${encodeURIComponent(String(photoId))}`;
    const image = document.createElement('img');
    image.className = 'w-100 rounded';
    image.src = getSafePhotoMediaUrl(
      photo.thumbnail_url,
      '/api/photo/thumbnail',
      getStaticPath('images/placeholder.jpg')
    );
    image.alt = String(photo.title || '照片');
    link.appendChild(image);
    col.appendChild(link);
    relatedPhotosContainer.appendChild(col);
  });
}
