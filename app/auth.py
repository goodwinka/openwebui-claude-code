from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

security = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> str:
    """Проверяет API-ключ из заголовка Authorization: Bearer <key>.

    Возвращает ключ при успешной проверке.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if credentials.credentials != settings.api_secret_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return credentials.credentials
