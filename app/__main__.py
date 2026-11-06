import uvicorn

from .config import Config
from .main import create_app


def main() -> None:
    config = Config()
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning", server_header=False, ws="none",
                proxy_headers=config.trust_proxy, forwarded_allow_ips="*" if config.trust_proxy else None)


if __name__ == "__main__":
    main()
