from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TSZ_AGENT_", env_file=".env", extra="ignore")

    database_url: SecretStr
    api_token: SecretStr
    tsz_base_url: AnyHttpUrl
    tsz_service_token: SecretStr
    deepseek_api_key: SecretStr | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    deepseek_model: Literal["deepseek-flash"] = "deepseek-flash"
    opencode_server_url: AnyHttpUrl | None = None
    opencode_server_password: SecretStr | None = None
    lease_seconds: int = Field(default=180, ge=30, le=1800)
    request_timeout_seconds: int = Field(default=90, ge=5, le=600)
    job_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    max_attempts: int = Field(default=3, ge=1, le=5)
    poll_seconds: float = Field(default=2, ge=0.1, le=60)
    model_request_limit: int = Field(default=4, ge=2, le=6)
    output_token_limit: int = Field(default=6000, ge=500, le=12000)

    @field_validator("database_url")
    @classmethod
    def database_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use postgresql+psycopg")
        return value

    @field_validator("api_token", "tsz_service_token")
    @classmethod
    def strong_service_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("service token must contain at least 32 characters")
        return value
