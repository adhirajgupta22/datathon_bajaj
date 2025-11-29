import os
from pydantic_settings import BaseSettings
from typing import Literal


class Settings(BaseSettings):
    gemini_api_key: str = ""
    llm_model: str = "gemini-2.0-flash"
    max_tokens: int = 8192
    temperature: float = 0.1
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    
    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
