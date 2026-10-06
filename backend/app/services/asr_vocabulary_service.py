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
    def _endpoint(self):
        """model_gateway 收口（2026-08-30）：热词表 REST 的 url/key/model
        统一来自 registry 的 asr 端点 extra。"""
        from app.model_gateway.registry import get_model_registry
        try:
            return get_model_registry().get("asr")
        except Exception:
            return None

    @property
    def _api_url(self) -> str:
        ep = self._endpoint
        if ep is not None and ep.extra.get("dashscope_vocabulary_url"):
            return ep.extra["dashscope_vocabulary_url"]
        return self._asr_config.get(
            "dashscope_vocabulary_url",
            "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/customization",
        )

    @property
    def _api_key(self) -> str:
        ep = self._endpoint
        if ep is not None:
            # 单一密钥原则（与 ASRService.dashscope_api_key 对齐）：顶层
            # api_key 即当前 provider 密钥；旧 extra.dashscope_api_key 兜底。
            return str(ep.api_key or "") or str(ep.extra.get("dashscope_api_key", "") or "")
        return self._asr_config.get("dashscope_api_key", "")

    @property
    def _target_model(self) -> str:
        """与 ASRService.dashscope_model 逐跳一致（词表 target_model ≠ 识别
        模型 = 热词静默失效；A4.9 R1-I1：终端回落曾分叉）。"""
        from app.services.asr_service import DEFAULT_DASSCOPE_ASR_MODEL

        ep = self._endpoint
        if ep is not None:
            resolved = str(getattr(ep, "model_name", "") or "") or str(
                ep.extra.get("dashscope_model", "") or ""
            )
            return resolved or DEFAULT_DASSCOPE_ASR_MODEL
        return self._asr_config.get("dashscope_model", DEFAULT_DASSCOPE_ASR_MODEL)

    @property
    def _prefix(self) -> str:
        ep = self._endpoint
        if ep is not None and ep.extra.get("vocabulary_prefix"):
            return ep.extra["vocabulary_prefix"]
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
        """Convert internal hotword dicts to the DashScope vocabulary format.
        权重 [1,5]（推荐 4）或 50（超级热词）。"""
        result = []
        for item in hotwords:
            weight = int(item.get("weight", 4) or 4)
            entry: dict = {
                "text": item["text"],
                "weight": 50 if weight == 50 else max(1, min(5, weight)),
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
        # 全量保真波（评审 M4）：热词属同步域——ORM 逐行更新触发捕获
        rows = (await db.execute(
            select(UserAsrHotword).where(UserAsrHotword.user_id == user_id)
        )).scalars().all()
        for row in rows:
            row.dashscope_vocabulary_id = vocabulary_id
        await db.commit()
