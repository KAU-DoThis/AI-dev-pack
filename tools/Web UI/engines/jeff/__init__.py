"""Jeff 엔진 (firelex/jeff). 서버 jeff-serve 를 HTTP로 부른다."""

from engines.jeff.client import JeffClient


def create(settings: dict) -> JeffClient:
    """settings = config/settings.yaml 의 engines.jeff (url·model·timeout)."""
    return JeffClient.from_settings(settings)
