from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/coincome"

    discord_bot_token: str = ""
    discord_guild_id: int | None = None

    upbit_access_key: str = ""
    upbit_secret_key: str = ""

    toss_client_key: str = ""
    toss_secret_key: str = ""

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    dashboard_base_url: str = "http://localhost:8000"

    secret_key: str = "change-me"


settings = Settings()
