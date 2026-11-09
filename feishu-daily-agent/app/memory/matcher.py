"""语义匹配器：embedding 向量模式与 jieba+TF-IDF 关键词模式。"""
import logging
import math
from typing import List, Tuple

import jieba
import numpy as np
from openai import OpenAI

from app.core.config import get_settings
from app.models import Experience

logger = logging.getLogger(__name__)


def tokenize(text: str) -> List[str]:
    return [w.strip() for w in jieba.lcut(text) if w.strip()]


class Matcher:
    def __init__(self) -> None:
        settings = get_settings()
        self.mode = settings.matcher_mode
        self._client: OpenAI | None = None
        self._model = settings.embedding_model
        if self.mode == "embedding":
            try:
                self._client = OpenAI(
                    base_url=settings.effective_embedding_base_url,
                    api_key=settings.effective_embedding_api_key,
                    timeout=settings.llm_timeout,
                )
            except Exception as exc:
                logger.warning("embedding 客户端初始化失败，降级为关键词模式: %s", exc)
                self.mode = "keyword"

    # ---------- embedding ----------

    def embed_one(self, text: str) -> List[float]:
        if self._client is None:
            raise RuntimeError("embedding 客户端不可用")
        resp = self._client.embeddings.create(model=self._model, input=[text])
        return list(resp.data[0].embedding)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    # ---------- 统一入口 ----------

    def match(
        self, query: str, experiences: List[Experience]
    ) -> List[Tuple[int, float]]:
        """返回按相似度降序的 (experience_id, score)；会就地补齐缺失向量。"""
        if not experiences:
            return []
        if self.mode == "embedding":
            try:
                return self._match_embedding(query, experiences)
            except Exception as exc:
                logger.warning("embedding 匹配失败，本次降级关键词模式: %s", exc)
        return self._match_keyword(query, experiences)

    def _match_embedding(
        self, query: str, experiences: List[Experience]
    ) -> List[Tuple[int, float]]:
        qv = np.array(self.embed_one(query))
        results: list[tuple[int, float]] = []
        for exp in experiences:
            vector = exp.get_embedding()
            if vector is None:
                vector = self.embed_one(exp.embedding_text or exp.question_pattern)
                exp.set_embedding(vector)  # 补齐持久化，由外层事务提交
            results.append((exp.id, self._cosine(qv, np.array(vector))))
        return sorted(results, key=lambda x: x[1], reverse=True)

    def _match_keyword(
        self, query: str, experiences: List[Experience]
    ) -> List[Tuple[int, float]]:
        docs = [
            tokenize(exp.embedding_text or exp.question_pattern)
            for exp in experiences
        ]
        q_tokens = tokenize(query)

        # 语料 document frequency 与 IDF
        df: dict[str, int] = {}
        for doc in docs:
            for word in set(doc):
                df[word] = df.get(word, 0) + 1
        n_docs = len(docs) + 1
        idf = {w: math.log((n_docs + 1) / (c + 1)) + 1 for w, c in df.items()}
        vocab = {w: i for i, w in enumerate(df)}

        def vectorize(tokens: List[str]) -> np.ndarray:
            v = np.zeros(len(vocab))
            tf: dict[str, int] = {}
            for w in tokens:
                if w in vocab:
                    tf[w] = tf.get(w, 0) + 1
            for w, count in tf.items():
                v[vocab[w]] = count * idf[w]
            norm = np.linalg.norm(v)
            return v / norm if norm else v

        qv = vectorize(q_tokens)
        results = [
            (exp.id, float(np.dot(qv, vectorize(doc))))
            for exp, doc in zip(experiences, docs)
        ]
        return sorted(results, key=lambda x: x[1], reverse=True)
