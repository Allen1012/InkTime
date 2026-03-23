from flask import Flask, render_template, request
import os

# 获取项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = Flask(__name__)

# 路由
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/photo/<int:photo_id>')
def photo(photo_id):
    return render_template('photo.html')

@app.route('/category')
def category():
    return render_template('category.html')

@app.route('/search')
def search():
    query = request.args.get('q', '')
    return render_template('search.html', query=query)

# 纯展示页面路由
@app.route('/display')
def display():
    return render_template('display.html')

@app.route('/display/<int:photo_id>')
def display_photo(photo_id):
    return render_template('display.html')

# API 路由
@app.route('/api/display/next')
def api_display_next():
    # 这里可以实现获取下一张照片的逻辑
    # 目前返回模拟数据
    return {
        'id': 2,
        'title': '照片 2',
        'caption': '这是下一张照片',
        'image_url': 'https://picsum.photos/800/600?random=2',
        'location': '上海',
        'date_taken': '2024-01-02T12:00:00'
    }

@app.route('/api/display/prev')
def api_display_prev():
    # 这里可以实现获取上一张照片的逻辑
    # 目前返回模拟数据
    return {
        'id': 1,
        'title': '照片 1',
        'caption': '这是上一张照片',
        'image_url': 'https://picsum.photos/800/600?random=1',
        'location': '北京',
        'date_taken': '2024-01-01T12:00:00'
    }

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)