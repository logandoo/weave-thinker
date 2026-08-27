# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import UserAsrHotword
from app.services.http_client import get_shared_async_client

logger = logging.getLogger(__name__)


class ASRVocabularyService:
    """Manage DashScope hot words vocabulary lists via the REST API.

    DashScope requires a two-step workflow for hot words:
    1. Create/update a vocabulary list via REST API to obtain a vocabulary_id.
    2. Pass vocabulary_id in the WebSocket run-task parameters when performing
       real-time speech recognition.

    This service handles step 1 and persists the vocabulary_id on the
    user_asr_hotwords rows so the ASR streaming service can read it in step 2.
    """

    def __init__(self):
        self._asr_config = get_config().asr

    @property
    def _api_url(self) -> str:
        return self._asr_config.get(
            "dashscope_vocabulary_url",
            "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/customization",
        )

    @property
    def _api_key(self) -> str:
        return self._asr_config.get("dashscope_api_key", "")

    @property
    def _target_model(self) -> str:
        return self._asr_config.get("dashscope_model", "fun-asr-realtime")

    @property
    def _prefix(self) -> str:
        return self._asr_config.get("vocabulary_prefix", "wvthinker")

    @property
    def _timeout(self) -> float:
        return float(self._asr_config.get("vocabulary_timeout_seconds", 30))

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _format_vocabulary(self, hotwords: list[dict]) -> list[dict]:
        """Convert internal hotword dicts to the DashScope vocabulary format."""
        result = []
        for item in hotwords:
            entry: dict = {
                "text": item["text"],
                "weight": max(1, min(5, item.get("weight", 4))),
            }
            if item.get("lang"):
                entry["lang"] = item["lang"]
            result.append(entry)
        return result

    async def sync(self, user_id: str, hotwords: list[dict], db: AsyncSession) -> str | None:
        """Synchronize the user's hotwords with DashScope.

        Returns the vocabulary_id that should be used in WebSocket run-task,
        or None if hot words are disabled or the list is empty.
        """
        if not self.enabled:
            logger.warning("DashScope vocabulary API key not configured, skipping sync")
            return None

        if not hotwords:
            await self._clear_vocabulary(user_id, db)
            return None

        vocabulary = self._format_vocabulary(hotwords)

        existing_id = await self._read_vocabulary_id(user_id, db)

        client = get_shared_async_client()
        if existing_id:
            ok = await self._update(client, existing_id, vocabulary)
            if ok:
                await self._write_vocabulary_id(user_id, db, existing_id)
                logger.info(
                    "Updated DashScope vocabulary %s for user %s (%d words)",
                    existing_id, user_id, len(vocabulary),
                )
                return existing_id
            logger.warning("Update failed for vocabulary %s, will recreate", existing_id)

        new_id = await self._create(client, vocabulary)
        if new_id:
            await self._write_vocabulary_id(user_id, db, new_id)
            logger.info(
                "Created DashScope vocabulary %s for user %s (%d words)",
                new_id, user_id, len(vocabulary),
            )
            return new_id

        logger.error("Failed to sync DashScope vocabulary for user %s", user_id)
        return None

    async def _create(self, client: httpx.AsyncClient, vocabulary: list[dict]) -> str | None:
        try:
            resp = await client.post(
                self._api_url,
                headers=self._headers(),
                json={
                    "model": "speech-biasing",
                    "input": {
                        "action": "create_vocabulary",
                        "target_model": self._target_model,
                        "prefix": self._prefix,
                        "vocabulary": vocabulary,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            vid = data.get("output", {}).get("vocabulary_id")
            if vid:
                return vid
            logger.error("DashScope create_vocabulary returned no vocabulary_id: %s", data)
        except Exception as e:
            logger.error("DashScope create_vocabulary failed: %s", e)
        return None

    async def _update(
        self, client: httpx.AsyncClient, vocabulary_id: str, vocabulary: list[dict]
    ) -> bool:
        try:
            resp = await client.post(
                self._api_url,
                headers=self._headers(),
                json={
                    "model": "speech-biasing",
                    "input": {
                        "action": "update_vocabulary",
                        "vocabulary_id": vocabulary_id,
                        "vocabulary": vocabulary,
                    },
                },
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("DashScope update_vocabulary failed for %s: %s", vocabulary_id, e)
            return False

    async def _delete(self, client: httpx.AsyncClient, vocabulary_id: str) -> bool:
        try:
            resp = await client.post(
                self._api_url,
                headers=self._headers(),
                json={
                    "model": "speech-biasing",
                    "input": {
                        "action": "delete_vocabulary",
                        "vocabulary_id": vocabulary_id,
                    },
                },
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("DashScope delete_vocabulary failed for %s: %s", vocabulary_id, e)
            return False

    async def _clear_vocabulary(self, user_id: str, db: AsyncSession) -> None:
        """Delete the DashScope vocabulary and clear the stored ID."""
        existing_id = await self._read_vocabulary_id(user_id, db)
        if existing_id and self.enabled:
            client = get_shared_async_client()
            await self._delete(client, existing_id)
        await self._write_vocabulary_id(user_id, db, None)

    async def _read_vocabulary_id(self, user_id: str, db: AsyncSession) -> str | None:
        result = await db.execute(
            select(UserAsrHotword.dashscope_vocabulary_id)
            .where(UserAsrHotword.user_id == user_id)
            .where(UserAsrHotword.dashscope_vocabulary_id.isnot(None))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _write_vocabulary_id(
        self, user_id: str, db: AsyncSession, vocabulary_id: str | None
    ) -> None:
        await db.execute(
            update(UserAsrHotword)
            .where(UserAsrHotword.user_id == user_id)
            .values(dashscope_vocabulary_id=vocabulary_id)
        )
        await db.commit()
