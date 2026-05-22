import os
from dotenv import load_dotenv
load_dotenv()

class Settings:
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://onemarinex_user:onemarinex123!@localhost:5432/onemarinex",
    )
    SECRET_KEY = os.getenv("SECRET_KEY", "onemarinexsecret")
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 20160  # 2 weeks (14 days * 24 hours * 60 minutes)
    REFRESH_TOKEN_EXPIRE_MINUTES = int(os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", "43200"))  # 30 days
    APP_NAME = "OneMarinex API"

settings = Settings()
