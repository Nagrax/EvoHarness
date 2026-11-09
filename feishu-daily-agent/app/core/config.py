"""全局配置：从 .env / 环境变量加载。

设计约束（对应 FEISHU-DELIVERY-PLAYBOOK §1）：
- 运行模式、采集许可、日报许可、回复许可彼此独立，不能由读权限推导写权限。
- 测试环境必须显式指向临时数据库，禁止误连运行中的业务库。
- 所有对外发送身份、数据保留期与模型预算由部署配置显式声明。
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Literal, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DeploymentMode = Literal["offline", "readonly_shadow", "single_approval", "whitelist_auto"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 飞书应用 ----
    feishu_app_id: str = Field("offline-app", alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field("offline-secret", alias="FEISHU_APP_SECRET")
    feishu_owner_open_id: str = Field("ou_owner", alias="FEISHU_OWNER_OPEN_ID")
    feishu_bot_open_id: str = Field("", alias="FEISHU_BOT_OPEN_ID")
    feishu_group_reply_only_mention: bool = Field(
        True, alias="FEISHU_GROUP_REPLY_ONLY_MENTION"
    )
    feishu_chat_whitelist: str = Field("", alias="FEISHU_CHAT_WHITELIST")
    feishu_chat_blacklist: str = Field("", alias="FEISHU_CHAT_BLACKLIST")
    feishu_reply_allowed_chats: str = Field("", alias="FEISHU_REPLY_ALLOWED_CHATS")
    feishu_reply_identity: Literal["bot", "user"] = Field("bot", alias="FEISHU_REPLY_IDENTITY")

    # ---- 租户与运行模式 ----
    tenant_id: str = Field("default", alias="FEISHU_TENANT_ID")
    deployment_mode: DeploymentMode = Field("offline", alias="FEISHU_DEPLOYMENT_MODE")
    policy_version: str = Field("policy-2026-11-06", alias="FEISHU_POLICY_VERSION")
    auto_reply_enabled: bool = Field(False, alias="FEISHU_AUTO_REPLY_ENABLED")
    shadow_mode: bool = Field(True, alias="FEISHU_SHADOW_MODE")

    # ---- LLM ----
    llm_base_url: str = Field("http://127.0.0.1:9", alias="LLM_BASE_URL")
    llm_api_key: str = Field("offline-key", alias="LLM_API_KEY")
    llm_model: str = Field("offline-model", alias="LLM_MODEL")
    llm_temperature: float = Field(0.3, alias="LLM_TEMPERATURE")
    llm_timeout: int = Field(30, alias="LLM_TIMEOUT")
    llm_prompt_revision: str = Field("prompt-2026-11-06", alias="LLM_PROMPT_REVISION")
    llm_daily_budget_calls: int = Field(500, alias="LLM_DAILY_BUDGET_CALLS")
    llm_max_concurrency: int = Field(2, alias="LLM_MAX_CONCURRENCY")
    allow_live_model: bool = Field(False, alias="FEISHU_ALLOW_LIVE_MODEL")

    # ---- Embedding / 匹配 ----
    embedding_base_url: str = Field("", alias="EMBEDDING_BASE_URL")
    embedding_api_key: str = Field("", alias="EMBEDDING_API_KEY")
    embedding_model: str = Field("text-embedding-3-small", alias="EMBEDDING_MODEL")
    matcher_mode: str = Field("keyword", alias="MATCHER_MODE")

    # ---- 置信度策略（阈值由 F00/F07 冻结后作为约束）----
    auto_reply_threshold: float = Field(0.85, alias="AUTO_REPLY_THRESHOLD")
    draft_min_threshold: float = Field(0.55, alias="DRAFT_MIN_THRESHOLD")
    exact_match_score: float = Field(0.95, alias="EXACT_MATCH_SCORE")
    experience_dismiss_limit: int = Field(2, alias="EXPERIENCE_DISMISS_LIMIT")
    negative_example_threshold: float = Field(0.80, alias="NEGATIVE_EXAMPLE_THRESHOLD")

    # ---- 自动回复硬门 ----
    auto_reply_max_per_chat_per_day: int = Field(3, alias="AUTO_REPLY_MAX_PER_CHAT_PER_DAY")
    auto_reply_allowed_categories: str = Field("faq", alias="AUTO_REPLY_ALLOWED_CATEGORIES")
    auto_reply_start_hour: int = Field(0, alias="AUTO_REPLY_START_HOUR")
    auto_reply_end_hour: int = Field(24, alias="AUTO_REPLY_END_HOUR")
    source_freshness_seconds: int = Field(900, alias="SOURCE_FRESHNESS_SECONDS")
    draft_ttl_hours: int = Field(72, alias="DRAFT_TTL_HOURS")

    # ---- 每日流水线 ----
    daily_report_time: str = Field("08:30", alias="DAILY_REPORT_TIME")
    daily_report_timezone: str = Field("Asia/Shanghai", alias="DAILY_REPORT_TIMEZONE")
    daily_lookback_days: int = Field(1, alias="DAILY_LOOKBACK_DAYS")
    daily_report_enabled: bool = Field(True, alias="FEISHU_DAILY_REPORT_ENABLED")
    daily_card_max_items: int = Field(6, alias="FEISHU_DAILY_CARD_MAX_ITEMS")
    daily_card_max_bytes: int = Field(24000, alias="FEISHU_DAILY_CARD_MAX_BYTES")

    # ---- 存储与治理 ----
    database_url: str = Field("sqlite:///data/agent.db", alias="DATABASE_URL")
    migration_backup_keep: int = Field(3, alias="FEISHU_MIGRATION_BACKUP_KEEP")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    raw_payload_retention_days: int = Field(7, alias="RAW_PAYLOAD_RETENTION_DAYS")
    business_retention_days: int = Field(180, alias="BUSINESS_RETENTION_DAYS")
    evoharness_root: str = Field("", alias="EVOHARNESS_ROOT")
    heartbeat_path: str = Field("data/heartbeat.json", alias="FEISHU_HEARTBEAT_PATH")
    heartbeat_stale_seconds: int = Field(300, alias="FEISHU_HEARTBEAT_STALE_SECONDS")
    outbox_backlog_alert_seconds: int = Field(900, alias="FEISHU_OUTBOX_BACKLOG_ALERT_SECONDS")
    inbox_replay_enabled: bool = Field(True, alias="FEISHU_INBOX_REPLAY_ENABLED")
    inbox_replay_window_seconds: int = Field(21600, alias="FEISHU_INBOX_REPLAY_WINDOW_SECONDS")
    inbox_replay_interval_seconds: int = Field(60, alias="FEISHU_INBOX_REPLAY_INTERVAL_SECONDS")
    inbox_replay_batch: int = Field(50, alias="FEISHU_INBOX_REPLAY_BATCH")
    inbox_backlog_alert: int = Field(1000, alias="FEISHU_INBOX_BACKLOG_ALERT")

    # ---- 测试隔离 ----
    test_mode: bool = Field(False, alias="FEISHU_TEST_MODE")
    test_db_prefix: str = Field("", alias="FEISHU_TEST_DB_PREFIX")

    @field_validator("matcher_mode")
    @classmethod
    def normalize_matcher_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("embedding", "keyword"):
            return "keyword"
        return v

    @field_validator("deployment_mode")
    @classmethod
    def normalize_mode(cls, v: str) -> str:
        value = str(v).strip().lower()
        allowed = {"offline", "readonly_shadow", "single_approval", "whitelist_auto"}
        if value not in allowed:
            raise ValueError(f"未知部署模式: {v}")
        return value

    @model_validator(mode="after")
    def reject_unsafe_live_combination(self):
        if self.deployment_mode == "offline":
            # 离线模式不接受真实发送；生产配置必须显式声明模式。
            object.__setattr__(self, "auto_reply_enabled", False)
            object.__setattr__(self, "shadow_mode", True)
        if self.deployment_mode == "readonly_shadow":
            object.__setattr__(self, "auto_reply_enabled", False)
            object.__setattr__(self, "shadow_mode", True)
        if self.deployment_mode == "single_approval":
            object.__setattr__(self, "auto_reply_enabled", False)
            object.__setattr__(self, "shadow_mode", False)
        if self.deployment_mode == "whitelist_auto" and not self.auto_reply_enabled:
            object.__setattr__(self, "shadow_mode", True)
        if self.auto_reply_start_hour < 0 or self.auto_reply_end_hour > 24:
            raise ValueError("自动回复时间窗必须在 0..24")
        if self.auto_reply_start_hour >= self.auto_reply_end_hour:
            raise ValueError("自动回复时间窗起点必须早于终点")
        if self.llm_daily_budget_calls < 0:
            raise ValueError("模型预算不能为负")
        return self

    @property
    def whitelist(self) -> List[str]:
        return [x.strip() for x in self.feishu_chat_whitelist.split(",") if x.strip()]

    @property
    def blacklist(self) -> List[str]:
        return [x.strip() for x in self.feishu_chat_blacklist.split(",") if x.strip()]

    @property
    def auto_reply_categories(self) -> List[str]:
        return [x.strip() for x in self.auto_reply_allowed_categories.split(",") if x.strip()]

    @property
    def reply_allowed_chats(self) -> List[str]:
        return [x.strip() for x in self.feishu_reply_allowed_chats.split(",") if x.strip()]

    def reply_allowed_for(self, chat_id: str) -> bool:
        return chat_id in self.reply_allowed_chats

    @property
    def effective_embedding_base_url(self) -> str:
        return self.embedding_base_url or self.llm_base_url

    @property
    def effective_embedding_api_key(self) -> str:
        return self.embedding_api_key or self.llm_api_key

    @property
    def writes_enabled(self) -> bool:
        """是否允许向飞书写出（回复/日报）。离线模式明确为否。"""
        if self.deployment_mode == "offline":
            return False
        if self.deployment_mode == "readonly_shadow":
            # 只允许向 owner 发日报，业务回复仅生成草稿。
            return True
        return True

    @property
    def business_reply_enabled(self) -> bool:
        return self.deployment_mode in {"single_approval", "whitelist_auto"}

    def is_whitelisted(self, chat_id: str) -> bool:
        """空白名单按关闭处理；需要全放行必须显式列出会话。"""
        if chat_id in self.blacklist:
            return False
        return chat_id in self.whitelist


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
