from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', env_file_encoding='utf-8')

    database_url: SecretStr | None = None
    postgres_host: str = '127.0.0.1'
    postgres_port: int = Field(default=15432, gt=0, le=65535)
    postgres_db: str = 'keitaro'
    postgres_user: str = 'keitaro'
    postgres_password: SecretStr = SecretStr('')
    keitaro_base_url: str = 'https://keitaro-for-tests.etolegko.com/admin_api/v1'
    keitaro_api_key: SecretStr = SecretStr('')
    keitaro_domain_id: int = Field(default=1813, gt=0)
    keitaro_group_id: int = Field(default=10332, gt=0)
    keitaro_traffic_source_id: int = Field(default=836, gt=0)
    keitaro_timeout: float = Field(default=15, gt=0, le=120)

    @property
    def connection_url(self):
        from sqlalchemy.engine import URL

        if self.database_url:
            return self.database_url.get_secret_value()
        return URL.create(
            'postgresql+psycopg', username=self.postgres_user,
            password=self.postgres_password.get_secret_value(), host=self.postgres_host,
            port=self.postgres_port, database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @field_validator('keitaro_base_url')
    @classmethod
    def validate_base_url(cls, value):
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.query:
            raise ValueError('Keitaro URL must use HTTPS without credentials or query parameters')
        return value.rstrip('/')
