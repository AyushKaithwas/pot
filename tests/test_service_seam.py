"""Where model access comes from.

An install from source talks to OpenAI with the user's own key and always will. The same build has
to be able to point at a service that holds the keys instead, because retrofitting that later means
touching every call site. Nothing above this layer should be able to tell the difference.
"""

from __future__ import annotations

from pot.config import ServiceConfig, Settings


def own_key() -> Settings:
    return Settings(openai_api_key="sk-mine")


def hosted() -> Settings:
    return Settings(
        openai_api_key="sk-mine",
        service_token="service-token",
        service=ServiceConfig(base_url="https://pot.example/v1"),
    )


def test_by_default_pot_talks_to_openai_with_your_own_key():
    settings = own_key()
    assert not settings.service.hosted
    assert settings.realtime_config() == {"api_key": "sk-mine"}
    assert settings.openai_client_args() == {"api_key": "sk-mine"}


def test_a_service_supplies_the_credential_instead():
    """The user's own key is ignored, so a hosted box doesn't need one at all."""
    settings = hosted()
    assert settings.model_token == "service-token"
    assert settings.openai_client_args()["base_url"] == "https://pot.example/v1"


def test_the_realtime_endpoint_follows_from_the_http_one():
    """One setting, not two: getting the websocket scheme wrong by hand is a silent failure."""
    assert hosted().realtime_config()["url"] == "wss://pot.example/v1/realtime"


def test_an_explicit_realtime_url_wins():
    settings = Settings(service=ServiceConfig(base_url="https://a/v1", realtime_url="wss://b/rt"))
    assert settings.realtime_config()["url"] == "wss://b/rt"


def test_a_plain_http_service_stays_unencrypted():
    """For someone running the relay on their own LAN, where https would just fail."""
    settings = Settings(service=ServiceConfig(base_url="http://192.168.1.5:8000/v1"))
    assert settings.realtime_config()["url"] == "ws://192.168.1.5:8000/v1/realtime"


def test_model_access_is_missing_when_neither_is_configured():
    assert not Settings().has_model_access
    assert own_key().has_model_access
    assert hosted().has_model_access


def test_a_service_token_alone_is_not_enough_without_an_endpoint():
    """A stray token shouldn't look like working access, or startup passes and every call 401s."""
    assert not Settings(service_token="orphan").has_model_access
