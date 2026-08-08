"""旧启动路径的兼容入口；实际应用初始化位于 src.server.app。"""

from __future__ import annotations

from src.server.app import create_app

__all__ = ["create_app"]


def main() -> None:
    """使用真正应用工厂启动 Flask 开发服务器。"""
    application = create_app()
    application.run(
        host=application.config["FLASK_HOST"],
        port=application.config["FLASK_PORT"],
        debug=False,
    )


if __name__ == "__main__":
    main()
