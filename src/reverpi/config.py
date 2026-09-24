from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=True, allow_inf_nan=False)


class RetryConfig(StrictModel):
    max_attempts: int = Field(3, ge=1, le=8)
    base_seconds: float = Field(1.0, ge=0, le=60)
    cap_seconds: float = Field(60, ge=0, le=3600)
    max_retry_after_seconds: float = Field(300, ge=0, le=86400)
    total_seconds: float = Field(600, gt=0, le=7200)
    retry_ambiguous: bool = False
    circuit_failures: int = Field(5, ge=1, le=100)
    circuit_cooldown_seconds: float = Field(60, gt=0, le=3600)


class Prices(StrictModel):
    # Frozen USER-provided rates, not a quotation of current provider prices.
    input_per_million: float = Field(0, ge=0, allow_inf_nan=False)
    cached_input_per_million: float = Field(0, ge=0, allow_inf_nan=False)
    output_per_million: float = Field(0, ge=0, allow_inf_nan=False)
    source: str = "UNCONFIGURED: no currency claims"
    configured: bool = False


class Provider(StrictModel):
    name: str = "provider"
    protocol: Literal["chat_completions", "responses"] = "chat_completions"
    base_url: str = "https://api.example.invalid/v1"
    api_key_env: str = "REVER_API_KEY"
    model: str = "REPLACE_WITH_MODEL_ID"
    expected_response_model: str | None = None
    effort: str = "low"
    effort_map: dict[str, str] = Field(default_factory=lambda: {x: x for x in ("low", "medium", "high", "xhigh", "max")})
    reasoning_style: Literal["standard", "thinking", "thinking_only"] = "standard"
    chat_output_field: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    max_output_tokens: int = Field(4096, ge=64, le=262144)
    context_window: int = Field(131072, ge=1024, le=4000000)
    context_admission: Literal["upstream", "byte_bound"] = "upstream"
    context_margin_tokens: int = Field(1024, ge=0)
    connect_seconds: float = Field(10, gt=0)
    read_seconds: float = Field(180, gt=0)
    write_seconds: float = Field(30, gt=0)
    pool_seconds: float = Field(30, gt=0)
    max_response_bytes: int = Field(16777216, ge=1024)
    stream: bool = False
    stream_options_usage: bool = True
    responses_include_encrypted_reasoning: bool = True
    supports_native_compaction: bool = False
    native_compact_output_reservation: int = Field(32768, ge=1024)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    extra_headers_env: dict[str, str] = Field(default_factory=dict)
    ca_bundle_env: str | None = None
    requests_per_minute: int = Field(30, ge=1)
    tokens_per_minute: int = Field(500000, ge=1024)
    concurrency: int = Field(2, ge=1, le=32)
    mock: bool = False
    retry: RetryConfig = Field(default_factory=RetryConfig)
    prices: Prices = Field(default_factory=Prices)

    @model_validator(mode="after")
    def check(self):
        for field in ("name", "model", "effort"):
            value = getattr(self, field)
            if not value.strip() or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(f"{field} must be a nonempty identifier without surrounding whitespace or control characters")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.base_url):
            raise ValueError("base_url contains whitespace or control characters")
        u = urlsplit(self.base_url)
        # urlsplit delays validation of malformed/out-of-range ports until access.
        _ = u.port
        if u.username or u.password or u.query or u.fragment:
            raise ValueError("base_url must not contain credentials, query strings or fragments")
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Provider HTTPS is required except for loopback test servers")
        if not u.hostname:
            raise ValueError("base_url requires a host")
        if self.effort not in self.effort_map:
            raise ValueError("effort lacks an explicit provider mapping")
        if any(v.strip().lower() in {"off", "none", "disabled", ""} for v in self.effort_map.values()):
            raise ValueError("This study is reasoning-only; no off/none mapping is allowed")
        reserved = {"model", "input", "messages", "tools", "tool_choice", "reasoning", "reasoning_effort", "thinking",
                    "max_tokens", "max_output_tokens", "max_completion_tokens", "stream", "stream_options",
                    "store", "previous_response_id", "include", "n", "temperature", "top_p", "context_management"}
        if reserved.intersection(self.extra_body):
            raise ValueError("extra_body cannot overwrite experiment/protocol controls")
        if len({k.lower() for k in self.extra_headers_env}) != len(self.extra_headers_env):
            raise ValueError("Duplicate case-insensitive HTTP header name")
        if any(k.lower() in {"authorization", "host", "content-length", "content-type", "cookie", "connection",
                            "transfer-encoding", "accept-encoding", "te", "trailer", "upgrade", "proxy-authorization"} for k in self.extra_headers_env):
            raise ValueError("Reserved header in extra_headers_env")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) for x in [self.api_key_env,*self.extra_headers_env.values(),*([self.ca_bundle_env] if self.ca_bundle_env else [])]):
            raise ValueError("Secret references must be environment variable names, not secret values")
        if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", h) for h in self.extra_headers_env):
            raise ValueError("Invalid HTTP header name")
        if self.protocol == "responses" and self.reasoning_style == "thinking_only":
            raise ValueError("thinking_only is a Chat-specific binary toggle, not effort control")
        if self.context_margin_tokens + self.max_output_tokens >= self.context_window:
            raise ValueError("No input capacity remains")
        return self

    def secret(self) -> str:
        if self.mock:
            return "mock-no-secret"
        val = os.environ.get(self.api_key_env)
        if not val:
            raise ValueError(f"Set environment variable {self.api_key_env}; never put keys in YAML")
        if "\n" in val or "\r" in val:
            raise ValueError("API key contains a newline")
        return val


class Budget(StrictModel):
    max_total_tokens: int = Field(2000000, ge=1)
    max_total_usd: float | None = Field(None, gt=0, allow_inf_nan=False)
    max_attempts: int = Field(500, ge=1)
    min_free_disk_bytes: int = Field(67108864, ge=0)
    per_cell_attempts: int = Field(300, ge=1)
    per_cell_tokens: int = Field(250000, ge=1)
    per_cell_usd: float | None = Field(None, gt=0, allow_inf_nan=False)


class CompressionConfig(StrictModel):
    memory_bytes: int = Field(18000, ge=512, le=4000000)
    recent_records: int = Field(4, ge=0, le=100)
    excerpt_chars: int = Field(400, ge=32, le=8192)
    archive_bytes: int = Field(20000000, ge=1024)
    max_metadata_bytes: int = Field(2000000, ge=1024)
    recovery_calls: int = Field(3, ge=0, le=20)
    recovery_chars: int = Field(6000, ge=100, le=100000)
    # Opt-in generic navigation; legacy head excerpts remain reproducible.
    recovery_search_mode: Literal["head", "match"] = "head"
    summarizer_output_tokens: int = Field(4096, ge=64, le=32768)
    hybrid_threshold_bytes: int = Field(12000, ge=256)
    guideline: str = "Preserve task goals, active constraints, unresolved work and version-qualified evidence."


class OnlineProjectionConfig(StrictModel):
    """Opt-in simple request-boundary candidate, not a model-capability claim."""
    mode: Literal["off", "observe", "apply"] = "off"
    # Explicit new-generation tool ABI. Legacy replay remains byte-identical.
    recovery_interface: Literal["legacy", "split_v1"] = "legacy"
    min_observation_bytes: int = Field(10240, ge=1024, le=4_000_000)
    excerpt_bytes: int = Field(1024, ge=128, le=8192)
    full_exposures: int = Field(2, ge=1, le=10)
    keep_recent_results: int = Field(1, ge=1, le=100)
    max_trace_events: int = Field(400, ge=1, le=10000)
    max_trace_bytes: int = Field(67_108_864, ge=1024, le=1_073_741_824)

    @model_validator(mode="after")
    def check_projection(self):
        if self.excerpt_bytes >= self.min_observation_bytes:
            raise ValueError("Projection excerpt must be smaller than the observation threshold")
        return self


class StudyConfig(StrictModel):
    name: str = "pilot"
    methods: list[str] = Field(default_factory=lambda: ["full", "mask", "archive", "rever_lite"])
    split: Literal["search", "dev", "gate", "external"] = "search"
    repeats: int = Field(1, ge=1, le=10)
    seed: int = 20260912
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    budget: Budget = Field(default_factory=Budget)
    actor_max_turns: int = Field(4, ge=1, le=32)
    gateway_body_seconds: float = Field(30, ge=0.01, le=300)
    early_response_headers: bool = False
    # JSON permits leading whitespace. Zero preserves the gen9 wire contract.
    response_heartbeat_seconds: float = Field(0, ge=0, le=60)
    online_projection: OnlineProjectionConfig = Field(default_factory=OnlineProjectionConfig)
    freeze_manifest: str | None = None
    code_sha: str | None = None

    @model_validator(mode="after")
    def check_methods(self):
        if self.online_projection.mode != "off":
            if set(self.methods) - {"pi_original", "mask"}:
                raise ValueError("Online v1 is one mask candidate with a pi_original baseline, not a new method sweep")
            if self.compression.recovery_calls < 1:
                raise ValueError("Online projection requires reachable archive recovery")
            if self.split in {"gate", "external"}:
                raise ValueError("Online v1 is a development candidate; no automatic external gate migration")
        if self.response_heartbeat_seconds and not self.early_response_headers:
            raise ValueError("Response heartbeats require early_response_headers")
        if self.response_heartbeat_seconds and self.response_heartbeat_seconds < 0.05:
            raise ValueError("Response heartbeat interval must be zero or at least 0.05 seconds")
        if not self.methods or len(set(self.methods)) != len(self.methods):
            raise ValueError("methods must be nonempty and unique")
        return self


class UniqueSafeLoader(yaml.SafeLoader):
    """No last-key-wins configuration, including keys introduced by YAML merges."""
    pass


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in mapping:
                raise ValueError("Duplicate configuration key; explicit unambiguous values are required")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise ValueError("Configuration mapping keys must be scalar") from exc
    return mapping


UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load(path: str | Path, cls: type[StrictModel]):
    try:
        with Path(path).open(encoding="utf-8") as f:
            raw = yaml.load(f, Loader=UniqueSafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("Invalid YAML configuration; source values are not echoed") from exc
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a mapping")
    return cls.model_validate(raw)
