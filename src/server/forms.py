"""后台认证使用的 Flask-WTF 表单。"""

from flask_wtf import FlaskForm
from wtforms import FloatField, HiddenField, PasswordField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, EqualTo, Length, NumberRange, Optional


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


class SetupForm(FlaskForm):
    """校验首次管理员设置字段，令牌和首位约束由认证服务最终检查。"""

    username = StringField(
        "用户名",
        validators=[DataRequired(message="请输入用户名"), Length(max=128)],
    )
    password = PasswordField(
        "密码",
        validators=[
            DataRequired(message="请输入密码"),
            Length(min=12, message="密码至少需要 12 个字符"),
        ],
    )
    confirm_password = PasswordField(
        "确认密码",
        validators=[
            DataRequired(message="请再次输入密码"),
            EqualTo("password", message="两次输入的密码不一致"),
        ],
    )
    setup_token = PasswordField(
        "初始化令牌",
        validators=[DataRequired(message="请输入初始化令牌")],
    )


class PhotoEditForm(FlaskForm):
    """校验后台照片编辑页面字段，业务一致性由服务层统一处理。"""

    version = HiddenField(validators=[DataRequired(message="缺少照片版本")])
    caption = TextAreaField("画面描述", validators=[Length(max=500)])
    side_caption = TextAreaField("旁白", validators=[Length(max=100)])
    memory_score = FloatField(
        "回忆分",
        validators=[Optional(), NumberRange(min=0, max=100)],
    )
    beauty_score = FloatField(
        "美观分",
        validators=[Optional(), NumberRange(min=0, max=100)],
    )
    reason = TextAreaField("评分理由", validators=[Length(max=1000)])
    exif_city = StringField("城市", validators=[Length(max=100)])
    category = StringField("分类")
    date_taken = StringField("拍摄日期时间")
    analysis_status = SelectField(
        "分析状态",
        choices=(
            ("legacy", "历史记录"),
            ("pending", "等待分析"),
            ("running", "分析中"),
            ("succeeded", "分析成功"),
            ("failed", "分析失败"),
        ),
    )
