// 测试前端获取照片数据
const fetch = require('node-fetch');

async function testFrontend() {
  try {
    // 测试 API 端点
    console.log('测试 /api/photos 端点...');
    const response = await fetch('http://localhost:5005/api/photos');
    const data = await response.json();
    
    console.log('API 响应状态:', data.status);
    console.log('总照片数:', data.data.total);
    console.log('返回的照片:', data.data.items);
    
    if (data.data.items.length > 0) {
      console.log('第一张照片 ID:', data.data.items[0].id);
      console.log('第一张照片标题:', data.data.items[0].title);
      console.log('第一张照片链接:', `/photo/${data.data.items[0].id}`);
    }
    
    console.log('测试完成，API 端点工作正常！');
  } catch (error) {
    console.error('测试失败:', error);
  }
}

testFrontend();
