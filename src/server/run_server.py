"""使用 Waitress 启动 InkTime Web 服务的统一生产入口。"""

from waitress import serve

from src.server.app import create_app


def main() -> None:
    """创建独立应用并按其最终配置启动 Waitress。"""
    application = create_app()
    serve(
        application,
        host=application.config["FLASK_HOST"],
        port=application.config["FLASK_PORT"],
    )


if __name__ == "__main__":
    main()
