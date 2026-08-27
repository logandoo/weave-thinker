# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import jwt
import bcrypt
import uuid
from datetime import datetime, timedelta
from typing import Optional
from app.core.config import get_config

config = get_config()

SECRET_KEY = config.security_jwt_secret_key
ALGORITHM = "HS256"


async def hash_password(password: str) -> str:
    salt = await asyncio.to_thread(bcrypt.gensalt)
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


async def verify_password(password: str, hashed: str) -> bool:
    return await asyncio.to_thread(
        bcrypt.checkpw, password.encode("utf-8"), hashed.encode("utf-8")
    )


def create_access_token(user_id: str, username: str) -> str:
    # Read the window at call time (was a module-level constant), so a config
    # change applies to newly issued tokens without a restart debate.
    days = config.security_token_expire_days
    expire = datetime.utcnow() + timedelta(days=days)
    payload = {
        "sub": user_id,
        "username": username,
        "exp": expire,
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4())
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
