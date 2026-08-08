"""后台认证使用的 Flask-WTF 表单。"""

from flask_wtf import FlaskForm
from wtforms import HiddenField, PasswordField, StringField
from wtforms.validators import DataRequired, Length


class LoginForm(FlaskForm):
    """校验管理员登录字段，认证失败原因由服务层统一隐藏。"""

    username = StringField(
        "用户名",
        validators=[DataRequired(message="请输入用户名"), Length(max=128)],
    )
    password = PasswordField(
        "密码",
        validators=[DataRequired(message="请输入密码")],
    )
    next = HiddenField()
