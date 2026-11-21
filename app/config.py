from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    BOT_TOKEN: str
    ADMIN_TELEGRAM_IDS: str = ""
    RTU_PASSWORD: str = "1234"
    RTU_PHONE: str = "+79990000000"
    WORKER_API_TOKEN: str
    ADMIN_WEB_PASSWORD: str = "admin123"
    PAYMENT_ADMIN_PASSWORD: str = "pay1234"
    WEBAPP_URL: str = ""
    BOT_PROXY: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    @property
    def admin_ids(self) -> list[int]:
        if not self.ADMIN_TELEGRAM_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_TELEGRAM_IDS.split(",") if x.strip().isdigit()]


settings = Settings()
