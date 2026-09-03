"""SentinelOS core configuration.

Loads credentials from the environment and pins every Binance client to
official Testnet hosts. Production endpoints are rejected with
RuntimeError (fail-closed).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from policies.testnet_only import ALLOWED_TESTNET_HOSTS, verify_testnet_environment

SPOT_BASE_URL: Final[str] = "https://testnet.binance.vision"
FUTURES_BASE_URL: Final[str] = "https://testnet.binancefuture.com"

_ALLOWED_SPOT_HOST: Final[str] = "testnet.binance.vision"
_ALLOWED_FUTURES_HOST: Final[str] = "testnet.binancefuture.com"

_PRODUCTION_HOST_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "api.binance.com",
        "api1.binance.com",
        "api2.binance.com",
        "api3.binance.com",
        "api4.binance.com",
        "fapi.binance.com",
        "dapi.binance.com",
        "stream.binance.com",
        "fstream.binance.com",
        "dstream.binance.com",
        "api.binance.us",
        "fapi.binance.us",
    }
)


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _reject_production_url(url: str) -> None:
    """Raise RuntimeError if *url* targets a production Binance host."""
    host = _hostname(url)
    looks_production = host in _PRODUCTION_HOST_MARKERS or (
        host.endswith("binance.com") and host not in ALLOWED_TESTNET_HOSTS
    )
    if looks_production or host not in ALLOWED_TESTNET_HOSTS:
        raise RuntimeError(
            f"Blocked non-Testnet Binance host: {host or url!r}. "
            "SentinelOS refuses production API endpoints."
        )
    verify_testnet_environment(url)


class Settings(BaseSettings):
    """Fail-closed Testnet settings for SentinelOS.

    Environment mapping (see ``.env.example``):

    - ``BINANCE_ENV`` must equal ``testnet``
    - ``BINANCE_SPOT_TESTNET_API_KEY`` / ``SPOT_API_KEY``
    - ``BINANCE_SPOT_TESTNET_API_SECRET`` / ``SPOT_API_SECRET``
    - ``BINANCE_FUTURES_TESTNET_API_KEY`` / ``FUTURES_API_KEY``
    - ``BINANCE_FUTURES_TESTNET_API_SECRET`` / ``FUTURES_API_SECRET``
    - ``LLM_API_KEY`` / ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``
    - ``LLM_MODEL`` (default ``auto`` — pick a live flash model via ``client.models.list()``)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
        populate_by_name=True,
    )

    binance_env: str = Field(
        default="testnet",
        validation_alias=AliasChoices("BINANCE_ENV", "binance_env"),
        description="Must equal 'testnet'. Any other value is rejected.",
    )

    spot_api_key: SecretStr = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "BINANCE_SPOT_TESTNET_API_KEY",
            "SPOT_API_KEY",
        ),
    )
    spot_api_secret: SecretStr = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "BINANCE_SPOT_TESTNET_API_SECRET",
            "SPOT_API_SECRET",
        ),
    )
    futures_api_key: SecretStr = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "BINANCE_FUTURES_TESTNET_API_KEY",
            "FUTURES_API_KEY",
        ),
    )
    futures_api_secret: SecretStr = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "BINANCE_FUTURES_TESTNET_API_SECRET",
            "FUTURES_API_SECRET",
        ),
    )
    llm_api_key: SecretStr = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "LLM_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "llm_api_key",
        ),
        description="Google Gemini API key from Google AI Studio.",
    )
    llm_model: str = Field(
        default="auto",
        min_length=1,
        validation_alias=AliasChoices("LLM_MODEL", "GEMINI_MODEL"),
        description="Gemini model id, or 'auto' to select a live flash model from the API catalog.",
    )

    spot_base_url: str = Field(
        default=SPOT_BASE_URL,
        validation_alias=AliasChoices("SPOT_BASE_URL", "BINANCE_SPOT_BASE_URL"),
    )
    futures_base_url: str = Field(
        default=FUTURES_BASE_URL,
        validation_alias=AliasChoices(
            "FUTURES_BASE_URL",
            "BINANCE_FUTURES_BASE_URL",
        ),
    )

    @field_validator("llm_model", mode="before")
    @classmethod
    def _require_gemini_model(cls, value: object) -> str:
        name = str(value).strip() if value is not None else ""
        if name.lower().startswith("models/"):
            name = name[7:].strip()
        if not name or name.lower() in {"auto", "dynamic", "latest"}:
            return "auto"
        return name

    @field_validator("binance_env", mode="before")
    @classmethod
    def _require_testnet_env(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized != "testnet":
            raise RuntimeError(
                f"BINANCE_ENV must equal 'testnet' (got {value!r}). "
                "SentinelOS is fail-closed and will not start against production."
            )
        return normalized

    @field_validator("spot_base_url", "futures_base_url", mode="before")
    @classmethod
    def _pin_testnet_base_urls(cls, value: object) -> str:
        url = str(value).strip().rstrip("/")
        _reject_production_url(url)
        return url

    @model_validator(mode="after")
    def _lock_official_testnet_hosts(self) -> Settings:
        if self.binance_env != "testnet":
            raise RuntimeError(
                "BINANCE_ENV must equal 'testnet'. "
                "SentinelOS refuses production operation."
            )

        spot_host = _hostname(self.spot_base_url)
        futures_host = _hostname(self.futures_base_url)

        if spot_host != _ALLOWED_SPOT_HOST:
            raise RuntimeError(
                f"SPOT_BASE_URL must use host {_ALLOWED_SPOT_HOST!r} "
                f"(got {spot_host!r}). Blocked non-Testnet Binance host."
            )
        if futures_host != _ALLOWED_FUTURES_HOST:
            raise RuntimeError(
                f"FUTURES_BASE_URL must use host {_ALLOWED_FUTURES_HOST!r} "
                f"(got {futures_host!r}). Blocked non-Testnet Binance host."
            )

        verify_testnet_environment(self.spot_base_url)
        verify_testnet_environment(self.futures_base_url)
        return self

    def require_testnet_endpoint(self, url: str) -> str:
        """Validate a client URL before any network call. Fail-closed."""
        _reject_production_url(url)
        return verify_testnet_environment(url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache process-wide settings. Raises RuntimeError on production."""
    settings = Settings()
    settings.require_testnet_endpoint(settings.spot_base_url)
    settings.require_testnet_endpoint(settings.futures_base_url)
    return settings
