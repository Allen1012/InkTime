"""创建由应用工厂延迟绑定的 Flask 扩展实例。"""

from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect


login_manager = LoginManager()
csrf = CSRFProtect()
