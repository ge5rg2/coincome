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

    # API 키 AES-256 암호화용 Fernet 키 (Base64 URL-safe 32바이트)
    # 생성: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    # 업비트 API 키 IP 화이트리스트에 등록할 서버 공인 IP
    server_ip: str = "0.0.0.0"

    # Anthropic API 키 (AI 자동 매매 펀드 매니저용 — claude-sonnet-4-6 채택)
    anthropic_api_key: str = ""

    # OpenAI API 키 (백테스트 비교용 — 운영 AI는 Anthropic 사용)
    openai_api_key: str = ""

    # Admin Dashboard API 키 (관리자 통계 엔드포인트 인증용)
    # 운영 환경에서 반드시 강력한 난수 값으로 설정 — 기본값 사용 금지
    admin_api_key: str = ""


settings = Settings()
