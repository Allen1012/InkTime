"""使用 Waitress 启动 InkTime Web 服务的统一生产入口。"""

from waitress import serve

from src.server.server import FLASK_HOST, FLASK_PORT, create_app


def main() -> None:
    """按环境配置启动 Waitress，供本地、systemd 与 Docker 共同调用。"""
    application = create_app()
    serve(application, host=FLASK_HOST, port=FLASK_PORT)


if __name__ == "__main__":
    main()
