#!/usr/bin/env python3
"""Antigravity CLI (agy) HUD Statusline Plugin.

Provides three core statusline enhancements for `agy`:
1. HUD-style visual context window progress bar with sub-block precision and dynamic color thresholds.
2. Session-level token usage breakdown (Input / Cache Read / Cache Write / Output / Thinking) and equivalent API cost in USD.
3. Token generation speed statistics (latest turn tok/s, session average tok/s, and TTFT).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any

# ---------------------------------------------------------------------------
# ANSI Styling & Visual Constants
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# 256-color palette (harmonized for Tokyo Night / dark terminals)
FG_MUTED = "\033[38;5;245m"
FG_LABEL = "\033[38;5;110m"
FG_TEXT = "\033[38;5;252m"
FG_CYAN = "\033[38;5;81m"
FG_GREEN = "\033[38;5;78m"
FG_YELLOW = "\033[38;5;220m"
FG_ORANGE = "\033[38;5;208m"
FG_RED = "\033[38;5;203m"
FG_MAGENTA = "\033[38;5;177m"
FG_GOLD = "\033[38;5;215m"
FG_BAR_EMPTY = "\033[38;5;238m"

# Fractional Unicode block elements from 0/8 to 8/8
BLOCKS = (" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█")


# ---------------------------------------------------------------------------
# Model Pricing & Context Defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """API pricing in USD per 1,000,000 tokens."""

    input_per_m: float
    output_per_m: float
    cache_write_per_m: float
    cache_read_per_m: float
    # Optional high-context (>200k tokens) pricing tier
    high_ctx_threshold: int = 0
    high_input_per_m: float = 0.0
    high_output_per_m: float = 0.0
    high_cache_write_per_m: float = 0.0
    high_cache_read_per_m: float = 0.0

    def compute_turn_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
    ) -> float:
        total_ctx = input_tokens + cache_read_tokens + cache_write_tokens
        if self.high_ctx_threshold > 0 and total_ctx > self.high_ctx_threshold:
            p_in = self.high_input_per_m or self.input_per_m
            p_out = self.high_output_per_m or self.output_per_m
            p_cw = self.high_cache_write_per_m or self.cache_write_per_m
            p_cr = self.high_cache_read_per_m or self.cache_read_per_m
        else:
            p_in = self.input_per_m
            p_out = self.output_per_m
            p_cw = self.cache_write_per_m
            p_cr = self.cache_read_per_m

        return (
            input_tokens * p_in
            + output_tokens * p_out
            + cache_write_tokens * p_cw
            + cache_read_tokens * p_cr
        ) / 1_000_000.0

    def compute_uncached_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
    ) -> float:
        """Equivalent cost if all cached reads were billed at regular input token rates."""
        total_in = input_tokens + cache_read_tokens + cache_write_tokens
        if self.high_ctx_threshold > 0 and total_in > self.high_ctx_threshold:
            p_in = self.high_input_per_m or self.input_per_m
            p_out = self.high_output_per_m or self.output_per_m
        else:
            p_in = self.input_per_m
            p_out = self.output_per_m
        return (total_in * p_in + output_tokens * p_out) / 1_000_000.0


# Standard API Pricing Table (USD / 1M tokens) aligned with agy built-in pricing & official API tiers
PRICING_GEMINI_PRO = ModelPricing(
    input_per_m=1.25,
    output_per_m=10.00,
    cache_write_per_m=1.25,
    cache_read_per_m=0.3125,
    high_ctx_threshold=200_000,
    high_input_per_m=2.50,
    high_output_per_m=15.00,
    high_cache_write_per_m=2.50,
    high_cache_read_per_m=0.625,
)

PRICING_GEMINI_FLASH = ModelPricing(
    input_per_m=0.30,
    output_per_m=2.50,
    cache_write_per_m=0.30,
    cache_read_per_m=0.0375,
)

PRICING_CLAUDE_SONNET = ModelPricing(
    input_per_m=3.00,
    output_per_m=15.00,
    cache_write_per_m=3.75,
    cache_read_per_m=0.30,
)

PRICING_CLAUDE_OPUS = ModelPricing(
    input_per_m=5.00,
    output_per_m=25.00,
    cache_write_per_m=6.25,
    cache_read_per_m=0.50,
)

PRICING_GPT_OSS = ModelPricing(
    input_per_m=0.50,
    output_per_m=2.00,
    cache_write_per_m=0.50,
    cache_read_per_m=0.125,
)


def get_model_pricing(model_name: str) -> ModelPricing:
    """Resolve API pricing table for an agy model ID or display name."""
    norm = (model_name or "").strip().lower()
    if "flash" in norm:
        return PRICING_GEMINI_FLASH
    if "sonnet" in norm:
        return PRICING_CLAUDE_SONNET
    if "opus" in norm:
        return PRICING_CLAUDE_OPUS
    if "gpt-oss" in norm or "llama" in norm:
        return PRICING_GPT_OSS
    # Default to Gemini Pro / Gemini 4 Argon pricing
    return PRICING_GEMINI_PRO


def get_default_context_window(model_name: str) -> int:
    """Fallback max context window size if neither stdin nor gen_metadata provides one."""
    norm = (model_name or "").strip().lower()
    if "claude" in norm or "sonnet" in norm or "opus" in norm:
        return 200_000
    if "gpt-oss" in norm:
        return 128_000
    return 256_000


# ---------------------------------------------------------------------------
# Protobuf Wire Parser for `gen_metadata` BLOBs
# ---------------------------------------------------------------------------


@dataclass
class TurnTelemetry:
    """Telemetry extracted from a single `gen_metadata` row (`CortexStepGeneratorMetadata`)."""

    idx: int = 0
    model_name: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_output_tokens: int = 0
    response_output_tokens: int = 0
    estimated_context_tokens: int = 0
    max_context_tokens: int = 0
    ttft_seconds: float = 0.0
    streaming_seconds: float = 0.0


def _read_varint(buf: bytes, pos: int, limit: int) -> tuple[int, int]:
    """Read a base-128 varint from `buf[pos:limit]`, returning `(value, next_pos)`."""
    result = 0
    shift = 0
    while pos < limit:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("Varint overflow")
    raise ValueError("Unexpected EOF while reading varint")


def _iter_proto_fields(buf: bytes, start: int = 0, end: int | None = None):
    """Yield `(field_number, wire_type, value_or_slice_bounds)` over a protobuf message."""
    pos = start
    limit = len(buf) if end is None else min(end, len(buf))
    while pos < limit:
        try:
            tag, pos = _read_varint(buf, pos, limit)
        except ValueError:
            break
        if tag == 0:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # Varint
            try:
                val, pos = _read_varint(buf, pos, limit)
            except ValueError:
                break
            yield field_num, wire_type, val
        elif wire_type == 1:  # 64-bit fixed
            if pos + 8 > limit:
                break
            pos += 8
        elif wire_type == 2:  # Length-delimited
            try:
                length, pos = _read_varint(buf, pos, limit)
            except ValueError:
                break
            if length < 0 or pos + length > limit:
                break
            yield field_num, wire_type, (pos, pos + length)
            pos += length
        elif wire_type == 5:  # 32-bit fixed
            if pos + 4 > limit:
                break
            pos += 4
        else:
            # Unknown or deprecated wire type; stop parsing current message safely
            break


def _parse_duration(buf: bytes, start: int, end: int) -> float:
    """Parse `google.protobuf.Duration` (field 1: seconds, field 2: nanos) into seconds."""
    seconds = 0
    nanos = 0
    for fnum, wtype, val in _iter_proto_fields(buf, start, end):
        if wtype == 0:
            # Handle signed int64 if high bit set (though durations here are non-negative)
            if val >= (1 << 63):
                val -= 1 << 64
            if fnum == 1:
                seconds = val
            elif fnum == 2:
                nanos = val
    return max(0.0, float(seconds) + float(nanos) / 1_000_000_000.0)


def parse_gen_metadata_blob(blob: bytes, idx: int = 0) -> TurnTelemetry:
    """Decode `CortexStepGeneratorMetadata` protobuf wire bytes into `TurnTelemetry`.

    Wire hierarchy in `agy`:
      CortexStepGeneratorMetadata:
        field 1 (ChatModelMetadata):
          field 4 (ModelUsageStats):
            field 2: input_tokens (varint)
            field 3: output_tokens (varint)
            field 4: cache_write_tokens (varint)
            field 5: cache_read_tokens (varint)
            field 9: thinking_output_tokens (varint)
            field 10: response_output_tokens (varint)
          field 9 (ChatStartMetadata):
            field 10 (ContextWindowMetadata):
              field 1: estimated_tokens_used (varint)
              field 4: max_context_tokens (varint)
          field 11 (google.protobuf.Duration): time_to_first_token
          field 12 (google.protobuf.Duration): streaming_duration
          field 19 (string): response_model
          field 21 (string): model_display_name
    """
    turn = TurnTelemetry(idx=idx)
    if not blob:
        return turn

    for top_fnum, top_wtype, top_val in _iter_proto_fields(blob):
        if top_fnum != 1 or top_wtype != 2:
            continue
        cm_start, cm_end = top_val
        for fnum, wtype, val in _iter_proto_fields(blob, cm_start, cm_end):
            if wtype == 2:
                s_start, s_end = val
                if fnum == 4:  # ModelUsageStats
                    for ufnum, uwtype, uval in _iter_proto_fields(blob, s_start, s_end):
                        if uwtype == 0:
                            if ufnum == 2:
                                turn.input_tokens = uval
                            elif ufnum == 3:
                                turn.output_tokens = uval
                            elif ufnum == 4:
                                turn.cache_write_tokens = uval
                            elif ufnum == 5:
                                turn.cache_read_tokens = uval
                            elif ufnum == 9:
                                turn.thinking_output_tokens = uval
                            elif ufnum == 10:
                                turn.response_output_tokens = uval
                elif fnum == 9:  # ChatStartMetadata
                    for cfnum, cwtype, cval in _iter_proto_fields(blob, s_start, s_end):
                        if cfnum == 10 and cwtype == 2:  # ContextWindowMetadata
                            cw_start, cw_end = cval
                            for wfnum, wwtype, wval in _iter_proto_fields(
                                blob, cw_start, cw_end
                            ):
                                if wwtype == 0:
                                    if wfnum == 1:
                                        turn.estimated_context_tokens = wval
                                    elif wfnum == 4:
                                        turn.max_context_tokens = wval
                elif fnum == 11:  # time_to_first_token
                    turn.ttft_seconds = _parse_duration(blob, s_start, s_end)
                elif fnum == 12:  # streaming_duration
                    turn.streaming_seconds = _parse_duration(blob, s_start, s_end)
                elif fnum in (19, 21):  # response_model / model_display_name
                    try:
                        text = blob[s_start:s_end].decode("utf-8", errors="ignore").strip()
                        if text and (fnum == 19 or not turn.model_name):
                            turn.model_name = text
                    except Exception:
                        pass
    return turn


# ---------------------------------------------------------------------------
# Session Aggregation & Incremental SQLite Cache
# ---------------------------------------------------------------------------


@dataclass
class SessionMetrics:
    """Aggregated session-level token, cost, context, and speed metrics."""

    turns_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_tokens: int = 0
    response_tokens: int = 0
    total_cost_usd: float = 0.0
    uncached_cost_usd: float = 0.0
    subagent_cost_usd: float = 0.0
    subagent_tokens: int = 0

    # Latest turn context window metadata from DB
    latest_estimated_context: int = 0
    latest_max_context: int = 0

    # Speed metrics
    last_turn_output_tokens: int = 0
    last_turn_ttft_sec: float = 0.0
    last_turn_stream_sec: float = 0.0
    last_turn_tps: float = 0.0
    last_stream_tps: float = 0.0
    total_gen_output_tokens: int = 0
    total_gen_duration_sec: float = 0.0
    avg_session_tps: float = 0.0

    # Internal incremental state
    max_idx: int = -1

    @property
    def total_input_context_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_context_tokens + self.output_tokens + self.subagent_tokens

    @property
    def cache_hit_ratio(self) -> float:
        total_in = self.total_input_context_tokens
        if total_in <= 0:
            return 0.0
        return (self.cache_read_tokens / total_in) * 100.0

    def add_turn(self, turn: TurnTelemetry, default_model: str = "") -> None:
        self.turns_count += 1
        self.input_tokens += turn.input_tokens
        self.output_tokens += turn.output_tokens
        self.cache_write_tokens += turn.cache_write_tokens
        self.cache_read_tokens += turn.cache_read_tokens
        self.thinking_tokens += turn.thinking_output_tokens
        self.response_tokens += turn.response_output_tokens

        model_name = turn.model_name or default_model
        pricing = get_model_pricing(model_name)
        self.total_cost_usd += pricing.compute_turn_cost(
            turn.input_tokens,
            turn.output_tokens,
            turn.cache_write_tokens,
            turn.cache_read_tokens,
        )
        self.uncached_cost_usd += pricing.compute_uncached_cost(
            turn.input_tokens,
            turn.output_tokens,
            turn.cache_write_tokens,
            turn.cache_read_tokens,
        )

        if turn.estimated_context_tokens > 0:
            self.latest_estimated_context = turn.estimated_context_tokens
        elif (turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens) > 0:
            self.latest_estimated_context = (
                turn.input_tokens + turn.cache_read_tokens + turn.cache_write_tokens
            )
        if turn.max_context_tokens > 0:
            self.latest_max_context = turn.max_context_tokens

        turn_dur = turn.ttft_seconds + turn.streaming_seconds
        if turn.output_tokens > 0 and turn_dur > 0.02:
            self.last_turn_output_tokens = turn.output_tokens
            self.last_turn_ttft_sec = turn.ttft_seconds
            self.last_turn_stream_sec = turn.streaming_seconds
            self.last_turn_tps = turn.output_tokens / turn_dur
            if turn.streaming_seconds >= 0.5 and turn.response_output_tokens >= 10:
                stream_tps = turn.response_output_tokens / turn.streaming_seconds
                # Only treat as token-by-token streaming if not a buffered tool-call flush
                self.last_stream_tps = stream_tps if stream_tps <= 250.0 else 0.0
            else:
                self.last_stream_tps = 0.0

            self.total_gen_output_tokens += turn.output_tokens
            self.total_gen_duration_sec += turn_dur
            self.avg_session_tps = (
                self.total_gen_output_tokens / self.total_gen_duration_sec
            )

        if turn.idx > self.max_idx:
            self.max_idx = turn.idx


def resolve_app_data_dir(stdin_data: dict[str, Any]) -> Path:
    """Determine the `~/.gemini/antigravity-cli` directory from env, transcript_path, or home."""
    env_dir = os.environ.get("AGY_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    transcript = stdin_data.get("transcript_path") or stdin_data.get("transcriptPath") or ""
    if transcript:
        p = Path(transcript)
        # Typically <appDataDir>/brain/<conv_id>/.system_generated/logs/transcript.jsonl
        for parent in p.parents:
            if (parent / "conversations").is_dir():
                return parent

    return Path.home() / ".gemini" / "antigravity-cli"


def _get_cache_path(conversation_id: str) -> Path:
    cache_root = os.environ.get("AGY_STATUSLINE_CACHE_DIR")
    if cache_root:
        base = Path(cache_root)
    else:
        uid = getattr(os, "getuid", lambda: getpass.getuser())()
        base = Path(tempfile.gettempdir()) / f"agy-statusline-{uid}"
    base.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", conversation_id)
    return base / f"{safe_id}.json"


def _open_sqlite_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.15)
    return conn


def _query_db_turns(
    db_path: Path, after_idx: int = -1
) -> tuple[int, int, list[TurnTelemetry]]:
    """Query `gen_metadata` rows with `idx > after_idx`.

    Returns `(row_count, max_idx, new_turns)`.
    """
    if not db_path.is_file():
        return 0, -1, []

    with _open_sqlite_ro(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(MAX(idx), -1) FROM gen_metadata")
        row = cur.fetchone()
        if not row or row[0] == 0:
            return 0, -1, []
        total_rows, max_idx = int(row[0]), int(row[1])
        if max_idx <= after_idx:
            return total_rows, max_idx, []

        cur.execute(
            "SELECT idx, data FROM gen_metadata WHERE idx > ? ORDER BY idx ASC",
            (after_idx,),
        )
        turns = [
            parse_gen_metadata_blob(blob, idx=int(idx))
            for idx, blob in cur.fetchall()
            if blob is not None
        ]
        return total_rows, max_idx, turns


def _find_subagent_conversation_ids(app_data_dir: Path, parent_conv_id: str) -> list[str]:
    """Look up child subagent conversation IDs from `conversation_summaries.db` if present."""
    summaries_db = app_data_dir / "conversation_summaries.db"
    if not summaries_db.is_file() or not parent_conv_id:
        return []
    try:
        with _open_sqlite_ro(summaries_db) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT conversation_id FROM conversation_summaries WHERE parent_conversation_id = ?",
                (parent_conv_id,),
            )
            return [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []


def load_session_metrics(
    conversation_id: str,
    app_data_dir: Path,
    default_model: str = "",
    use_cache: bool = True,
) -> SessionMetrics:
    """Load and aggregate `SessionMetrics` for `conversation_id` with incremental caching."""
    metrics = SessionMetrics()
    if not conversation_id:
        return metrics

    db_path = app_data_dir / "conversations" / f"{conversation_id}.db"
    if not db_path.is_file():
        return metrics

    cache_path = _get_cache_path(conversation_id) if use_cache else None
    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("version") == 1:
                m_data = cached.get("metrics", {})
                metrics = SessionMetrics(
                    **{
                        k: v
                        for k, v in m_data.items()
                        if k in SessionMetrics.__dataclass_fields__
                    }
                )
        except Exception:
            metrics = SessionMetrics()

    try:
        total_rows, max_idx, new_turns = _query_db_turns(
            db_path, after_idx=metrics.max_idx
        )
        # If the database was reset or compacted (fewer rows than cached), rebuild from scratch
        if total_rows < metrics.turns_count or max_idx < metrics.max_idx:
            metrics = SessionMetrics()
            _, max_idx, new_turns = _query_db_turns(db_path, after_idx=-1)

        for turn in new_turns:
            metrics.add_turn(turn, default_model=default_model)
    except Exception:
        return metrics

    # Aggregate any child subagents
    sub_ids = _find_subagent_conversation_ids(app_data_dir, conversation_id)
    if sub_ids:
        sub_cost = 0.0
        sub_tok = 0
        for sub_id in sub_ids:
            sub_db = app_data_dir / "conversations" / f"{sub_id}.db"
            if not sub_db.is_file():
                continue
            try:
                _, _, sub_turns = _query_db_turns(sub_db, after_idx=-1)
                sub_metrics = SessionMetrics()
                for st in sub_turns:
                    sub_metrics.add_turn(st, default_model=default_model)
                sub_cost += sub_metrics.total_cost_usd
                sub_tok += sub_metrics.total_tokens
            except Exception:
                continue
        metrics.subagent_cost_usd = sub_cost
        metrics.subagent_tokens = sub_tok

    if cache_path and new_turns:
        try:
            payload = {"version": 1, "metrics": asdict(metrics)}
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    return metrics


# ---------------------------------------------------------------------------
# Formatting & HUD Rendering
# ---------------------------------------------------------------------------


def format_tokens(count: int | float) -> str:
    """Format token count into compact human-readable form (e.g. `196.7k`, `4.82M`, `850`)."""
    n = max(0, int(round(count)))
    if n >= 1_000_000:
        val = n / 1_000_000.0
        return f"{val:.2f}M" if val < 10 else f"{val:.1f}M"
    if n >= 1_000:
        val = n / 1_000.0
        return f"{val:.1f}k" if val < 100 else f"{val:.0f}k"
    return str(n)


def format_usd(amount: float) -> str:
    """Format USD cost cleanly across small and large sessions."""
    val = max(0.0, float(amount))
    if val == 0.0:
        return "$0.00"
    if val < 0.01:
        return f"${val:.4f}"
    if val < 1.00:
        return f"${val:.3f}"
    return f"${val:.2f}"


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences to compute visible character length."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def context_color(percentage: float) -> str:
    """Select HUD color based on context window utilization percentage."""
    if percentage >= 90.0:
        return FG_RED
    if percentage >= 75.0:
        return FG_ORANGE
    if percentage >= 50.0:
        return FG_YELLOW
    return FG_GREEN


def render_hud_bar(percentage: float, width: int = 10) -> str:
    """Render a high-resolution Unicode block progress bar with ANSI color."""
    pct = max(0.0, min(100.0, float(percentage)))
    width = max(4, int(width))
    color = context_color(pct)

    total_eighths = int(round((pct / 100.0) * width * 8))
    full_cells = min(width, total_eighths // 8)
    remainder = total_eighths % 8 if full_cells < width else 0
    empty_cells = max(0, width - full_cells - (1 if remainder > 0 else 0))

    filled_str = "█" * full_cells
    partial_str = BLOCKS[remainder] if remainder > 0 else ""
    empty_str = "░" * empty_cells

    return (
        f"{FG_MUTED}[{RESET}"
        f"{color}{filled_str}{partial_str}{RESET}"
        f"{FG_BAR_EMPTY}{empty_str}{RESET}"
        f"{FG_MUTED}]{RESET}"
    )


def mode_color(cycle_mode: str) -> str:
    """Select badge color for `cycle_mode` matching agy built-in themes."""
    m = (cycle_mode or "").strip().lower()
    if m == "accept-edits":
        return FG_GREEN
    if m == "plan":
        return FG_CYAN
    if m == "auto":
        return FG_YELLOW
    if m == "best-of-n":
        return FG_MAGENTA
    return FG_LABEL


def build_native_section(
    stdin_data: dict[str, Any],
    compact: bool = False,
    include_vcs: bool = True,
) -> str:
    """Build native agy statusline section (Vim mode, Agent, Model + Effort, Cycle Mode, VCS, Tasks/Subagents)."""
    parts: list[str] = []

    # 1. Vim mode badge (if active)
    vim_info = stdin_data.get("vim") or {}
    vim_mode = str(vim_info.get("mode") or "").strip().upper()
    if vim_mode:
        vim_col = FG_GREEN if "INSERT" in vim_mode else (FG_MAGENTA if "VISUAL" in vim_mode else FG_YELLOW)
        parts.append(f"{vim_col}{BOLD}-- {vim_mode} --{RESET}" if not compact else f"{vim_col}{BOLD}[{vim_mode[0]}]{RESET}")

    # 2. Model display name + Effort badge
    model_info = stdin_data.get("model") or {}
    display_name = str(
        model_info.get("display_name") or model_info.get("id") or "Gemini 4 Argon"
    ).strip()
    effort = str(model_info.get("effort") or "").strip()

    agent_info = stdin_data.get("agent") or {}
    agent_name = str(agent_info.get("name") or "").strip()
    agent_prefix = f"{FG_CYAN}@{agent_name}{RESET} " if agent_name else ""

    if effort and f"({effort.lower()})" not in display_name.lower():
        effort_str = effort[0].upper() if compact else effort
        model_str = f"{agent_prefix}{FG_TEXT}{BOLD}{display_name}{RESET} {FG_MUTED}({effort_str}){RESET}"
    else:
        model_str = f"{agent_prefix}{FG_TEXT}{BOLD}{display_name}{RESET}"
    parts.append(model_str)

    # 3. Cycle mode (accept-edits, plan, auto, best-of-n)
    cycle_mode = str(stdin_data.get("cycle_mode") or "").strip()
    if cycle_mode:
        m_col = mode_color(cycle_mode)
        parts.append(f"{m_col}{cycle_mode}{RESET}")

    # 4. VCS branch (optional when in a git workspace and space permits)
    if include_vcs and not compact:
        vcs_info = stdin_data.get("vcs") or {}
        branch = str(vcs_info.get("branch") or "").strip()
        if branch:
            dirty = f"{FG_YELLOW}*{RESET}" if vcs_info.get("dirty") else ""
            parts.append(f"{FG_MUTED}⎇ {branch}{dirty}{RESET}")

    # 5. Subagents & background tasks (only shown when > 0, matching native renderRight)
    subagents = stdin_data.get("subagents") or []
    if isinstance(subagents, list) and len(subagents) > 0:
        n_sub = len(subagents)
        label = f"{n_sub} sub" if compact else f"{n_sub} subagent{'s' if n_sub != 1 else ''}"
        parts.append(f"{FG_CYAN}{label}{RESET}")

    task_count = int(stdin_data.get("task_count") or 0)
    if task_count > 0:
        label = f"{task_count}t" if compact else f"{task_count} task{'s' if task_count != 1 else ''}"
        parts.append(f"{FG_YELLOW}{label}{RESET}")

    dot = f" {FG_MUTED}·{RESET} "
    return dot.join(parts)


PRESET_ALIASES = {
    "minimal": "minimal",
    "min": "minimal",
    "compact": "minimal",
    "简洁": "minimal",
    "standard": "standard",
    "std": "standard",
    "default": "standard",
    "标准": "standard",
    "detailed": "detailed",
    "detail": "detailed",
    "full": "detailed",
    "详细": "detailed",
}


def normalize_preset(preset: str | None) -> str:
    """Normalize preset name (`minimal`, `standard`, `detailed`)."""
    raw = (preset or os.environ.get("AGY_STATUSLINE_PRESET") or "standard").strip().lower()
    return PRESET_ALIASES.get(raw, "standard")


def quota_color(remaining_pct: float) -> str:
    """Select HUD color based on remaining quota percentage (higher is better)."""
    if remaining_pct < 10.0:
        return FG_RED
    if remaining_pct < 25.0:
        return FG_ORANGE
    if remaining_pct < 50.0:
        return FG_YELLOW
    return FG_GREEN


def format_reset_duration(seconds: int | float | None) -> str:
    """Format quota reset countdown seconds into compact human string (e.g. `29m`, `4h32m`, `6d14h`)."""
    if seconds is None:
        return ""
    sec = int(seconds)
    if sec <= 0:
        return ""
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        h = sec // 3600
        m = (sec % 3600) // 60
        return f"{h}h{m}m" if m > 0 else f"{h}h"
    d = sec // 86400
    h = (sec % 86400) // 3600
    return f"{d}d{h}h" if h > 0 else f"{d}d"


def _select_quota_buckets(
    quota_map: dict[str, Any], model_name: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Select `(bucket_5h, bucket_weekly)` matching the active model family."""
    if not isinstance(quota_map, dict) or not quota_map:
        return None, None

    norm_model = (model_name or "").strip().lower()
    is_third_party = any(
        k in norm_model for k in ("claude", "sonnet", "opus", "gpt", "llama", "3p")
    )
    preferred_prefix = "3p" if is_third_party else "gemini"
    fallback_prefix = "gemini" if is_third_party else "3p"

    b_5h = (
        quota_map.get(f"{preferred_prefix}-5h")
        or quota_map.get("5h")
        or quota_map.get(f"{fallback_prefix}-5h")
    )
    b_wk = (
        quota_map.get(f"{preferred_prefix}-weekly")
        or quota_map.get("weekly")
        or quota_map.get(f"{fallback_prefix}-weekly")
    )

    if b_5h is None or b_wk is None:
        for k, v in quota_map.items():
            if not isinstance(v, dict):
                continue
            kl = k.lower()
            if b_5h is None and "5h" in kl:
                b_5h = v
            elif b_wk is None and ("week" in kl or "7d" in kl or "wk" in kl):
                b_wk = v

    return (
        b_5h if isinstance(b_5h, dict) and not b_5h.get("disabled") else None,
        b_wk if isinstance(b_wk, dict) and not b_wk.get("disabled") else None,
    )


def _format_single_quota(
    label: str, bucket: dict[str, Any] | None, show_reset: bool = True
) -> str:
    if not bucket or bucket.get("remaining_fraction") is None:
        return ""
    rem_pct = max(0.0, min(100.0, float(bucket["remaining_fraction"]) * 100.0))
    col = quota_color(rem_pct)
    reset_str = ""
    if show_reset:
        dur = format_reset_duration(bucket.get("reset_in_seconds"))
        if dur:
            reset_str = f"{FG_MUTED}({dur}){RESET}"
    return f"{FG_MUTED}{label}:{RESET}{col}{BOLD}{rem_pct:.0f}%{RESET}{reset_str}"


def build_quota_section(
    stdin_data: dict[str, Any],
    model_name: str,
    compact: bool = False,
) -> str:
    """Build Section 4 (Detailed preset): 5h quota and weekly quota remaining & reset time."""
    quota_map = stdin_data.get("quota") or {}
    b_5h, b_wk = _select_quota_buckets(quota_map, model_name)
    if not b_5h and not b_wk:
        return ""

    parts: list[str] = []
    q5 = _format_single_quota("5h", b_5h, show_reset=not compact)
    if q5:
        parts.append(q5)
    qwk = _format_single_quota("Wk", b_wk, show_reset=not compact)
    if qwk:
        parts.append(qwk)

    if not parts:
        return ""

    head = f"{FG_LABEL}Q{RESET}" if compact else f"{FG_LABEL}Quota{RESET}"
    return f"{head} {' '.join(parts)}"


def build_context_section(
    stdin_data: dict[str, Any],
    metrics: SessionMetrics,
    model_name: str,
    bar_width: int = 10,
    compact: bool = False,
    minimal: bool = False,
) -> str:
    """Build Section 1: HUD-style visual context progress bar."""
    cw = stdin_data.get("context_window") or {}
    cur_usage = cw.get("current_usage") or {}

    # 1. Resolve current context tokens used
    used_tokens = int(cw.get("total_input_tokens") or 0)
    if used_tokens <= 0 and metrics.latest_estimated_context > 0:
        used_tokens = metrics.latest_estimated_context
    if used_tokens <= 0:
        used_tokens = (
            int(cur_usage.get("input_tokens") or 0)
            + int(cur_usage.get("cache_read_input_tokens") or 0)
            + int(cur_usage.get("cache_creation_input_tokens") or 0)
        )

    # 2. Resolve max context window size
    max_tokens = int(cw.get("context_window_size") or 0)
    if max_tokens <= 0 and metrics.latest_max_context > 0:
        max_tokens = metrics.latest_max_context
    if max_tokens <= 0:
        max_tokens = get_default_context_window(model_name)

    # 3. Compute percentage
    if max_tokens > 0 and used_tokens > 0:
        pct = (used_tokens / max_tokens) * 100.0
    elif cw.get("used_percentage"):
        pct = float(cw.get("used_percentage") or 0.0)
    else:
        pct = 0.0

    bar = render_hud_bar(pct, width=bar_width)
    color = context_color(pct)
    warn = f" {FG_RED}⚠{RESET}" if pct >= 90.0 else ""

    if minimal:
        return f"{FG_LABEL}CTX{RESET} {bar} {color}{BOLD}{pct:.0f}%{RESET}{warn}"

    if compact:
        return (
            f"{FG_LABEL}CTX{RESET} {bar} "
            f"{color}{BOLD}{pct:.0f}%{RESET} "
            f"{FG_MUTED}{format_tokens(used_tokens)}/{format_tokens(max_tokens)}{RESET}{warn}"
        )

    return (
        f"{FG_LABEL}CTX{RESET} {bar} "
        f"{color}{BOLD}{pct:.1f}%{RESET} "
        f"{FG_MUTED}({format_tokens(used_tokens)}/{format_tokens(max_tokens)}){RESET}{warn}"
    )


def build_usage_and_cost_section(
    stdin_data: dict[str, Any],
    metrics: SessionMetrics,
    model_name: str,
    compact: bool = False,
    minimal: bool = False,
) -> str:
    """Build Section 2: Slimmed session-level token total, cache hit rate, and equivalent API cost."""
    cw = stdin_data.get("context_window") or {}
    cur = cw.get("current_usage") or {}

    if metrics.turns_count > 0:
        total_tok = metrics.total_tokens
        cache_ratio = metrics.cache_hit_ratio
        cost_usd = metrics.total_cost_usd + metrics.subagent_cost_usd
    else:
        # Fallback to stdin current_usage / context_window when DB has no rows yet
        in_uncached = int(cur.get("input_tokens") or 0)
        cache_read = int(cur.get("cache_read_input_tokens") or 0)
        cache_write = int(cur.get("cache_creation_input_tokens") or 0)
        out_total = int(cw.get("total_output_tokens") or cur.get("output_tokens") or 0)
        if in_uncached == 0 and cache_read == 0:
            in_uncached = int(cw.get("total_input_tokens") or 0)
        total_in = in_uncached + cache_read + cache_write
        total_tok = total_in + out_total
        cache_ratio = (cache_read / total_in * 100.0) if total_in > 0 else 0.0
        pricing = get_model_pricing(model_name)
        cost_usd = pricing.compute_turn_cost(
            in_uncached, out_total, cache_write, cache_read
        )

    # Check if stdin provides a higher explicit cost
    stdin_cost = stdin_data.get("cost") or {}
    explicit_usd = float(stdin_cost.get("total_usd") or 0.0)
    if explicit_usd > cost_usd:
        cost_usd = explicit_usd

    cost_str = f"{FG_GOLD}{BOLD}💰 {format_usd(cost_usd)}{RESET}"

    if minimal:
        return (
            f"{FG_LABEL}Σ{RESET} {FG_TEXT}{BOLD}{format_tokens(total_tok)}{RESET} "
            f"{FG_MUTED}·{RESET} {cost_str}"
        )

    if compact:
        cache_str = f" {FG_MUTED}(C:{cache_ratio:.0f}%){RESET}" if cache_ratio > 0 else ""
        return (
            f"{FG_LABEL}Σ{RESET} {FG_TEXT}{BOLD}{format_tokens(total_tok)}{RESET}"
            f"{cache_str} {FG_MUTED}·{RESET} {cost_str}"
        )

    cache_str = f" {FG_MUTED}(Cache {cache_ratio:.0f}%){RESET}" if cache_ratio > 0 else ""
    return (
        f"{FG_LABEL}Σ{RESET} {FG_TEXT}{BOLD}{format_tokens(total_tok)}{RESET}"
        f"{cache_str} {FG_MUTED}·{RESET} {cost_str}"
    )


def build_speed_section(
    stdin_data: dict[str, Any],
    metrics: SessionMetrics,
) -> str:
    """Build Section 3: Slimmed last-turn token generation speed (tok/s only)."""
    agent_state = (stdin_data.get("agent_state") or "").lower()
    is_working = agent_state in ("working", "thinking", "streaming", "running")
    spin_badge = f" {FG_CYAN}⟳{RESET}" if is_working else ""

    if metrics.last_turn_tps <= 0.0:
        state_txt = "generating..." if is_working else "-- tok/s"
        return f"{FG_CYAN}⚡{RESET} {FG_MUTED}{state_txt}{RESET}{spin_badge}"

    tps_color = (
        FG_GREEN
        if metrics.last_turn_tps >= 30.0
        else (FG_CYAN if metrics.last_turn_tps >= 15.0 else FG_YELLOW)
    )
    return f"{FG_CYAN}⚡{RESET} {tps_color}{BOLD}{metrics.last_turn_tps:.1f} tok/s{RESET}{spin_badge}"


def render_statusline(
    stdin_data: dict[str, Any],
    app_data_dir: Path | None = None,
    use_cache: bool = True,
    preset: str | None = None,
) -> str:
    """Render the unified single-line HUD statusline string from `StatusLineData`.

    Presets:
      - `minimal` (简洁): Compact model/mode, 8-cell context bar + %, total tokens + cost, speed.
      - `standard` (标准, default): Full native info, 10-cell context bar + (used/max), total tokens + Cache% + cost, speed.
      - `detailed` (详细): Everything in `standard` + 5h quota & weekly quota remaining% and reset countdown.
    """
    if app_data_dir is None:
        app_data_dir = resolve_app_data_dir(stdin_data)

    active_preset = normalize_preset(preset)
    conv_id = str(stdin_data.get("conversation_id") or "")
    model_info = stdin_data.get("model") or {}
    model_id = str(model_info.get("id") or model_info.get("display_name") or "gemini-4-argon")
    term_width = int(stdin_data.get("terminal_width") or 120)

    metrics = load_session_metrics(
        conversation_id=conv_id,
        app_data_dir=app_data_dir,
        default_model=model_id,
        use_cache=use_cache,
    )

    sep = f"  {FG_MUTED}│{RESET}  "
    tight_sep = f" {FG_MUTED}│{RESET} "
    speed_part = build_speed_section(stdin_data, metrics)

    # 1. Minimal preset (简洁模式)
    if active_preset == "minimal":
        native_min = build_native_section(stdin_data, compact=True, include_vcs=False)
        ctx_min = build_context_section(
            stdin_data, metrics, model_id, bar_width=8, minimal=True
        )
        usage_min = build_usage_and_cost_section(
            stdin_data, metrics, model_id, minimal=True
        )
        line_min = f"{native_min}{sep}{ctx_min}{sep}{usage_min}{sep}{speed_part}"
        if term_width <= 0 or visible_len(line_min) <= term_width:
            return line_min
        tight_min = f"{native_min}{tight_sep}{ctx_min}{tight_sep}{usage_min}{tight_sep}{speed_part}"
        if visible_len(tight_min) <= term_width:
            return tight_min
        return f"{native_min}{tight_sep}{speed_part}\n{ctx_min}{tight_sep}{usage_min}"

    # Optional quota section for `detailed` preset (详细模式)
    quota_full = (
        build_quota_section(stdin_data, model_id, compact=False)
        if active_preset == "detailed"
        else ""
    )
    quota_compact = (
        build_quota_section(stdin_data, model_id, compact=True)
        if active_preset == "detailed"
        else ""
    )

    # Tier 1: Wide single-line HUD
    native_full = build_native_section(stdin_data, compact=False, include_vcs=True)
    ctx_full = build_context_section(stdin_data, metrics, model_id, bar_width=10, compact=False)
    usage_full = build_usage_and_cost_section(stdin_data, metrics, model_id, compact=False)
    full_parts = [native_full, ctx_full, usage_full, speed_part]
    if quota_full:
        full_parts.append(quota_full)
    full_line = sep.join(full_parts)

    if term_width <= 0 or visible_len(full_line) <= term_width:
        return full_line

    # Tier 2: Standard single-line HUD (tighter separators, omit VCS branch if needed)
    native_no_vcs = build_native_section(stdin_data, compact=False, include_vcs=False)
    std_parts = [native_no_vcs, ctx_full, usage_full, speed_part]
    if quota_full:
        std_parts.append(quota_full)
    std_line = tight_sep.join(std_parts)
    if visible_len(std_line) <= term_width:
        return std_line

    # Tier 2b (Detailed mode): keep standard sections + compact quota
    if quota_compact:
        std_q_compact = tight_sep.join([native_no_vcs, ctx_full, usage_full, speed_part, quota_compact])
        if visible_len(std_q_compact) <= term_width:
            return std_q_compact

    # Tier 3: Compact single-line HUD (8-cell bar, compact labels)
    native_compact = build_native_section(stdin_data, compact=True, include_vcs=False)
    ctx_compact = build_context_section(stdin_data, metrics, model_id, bar_width=8, compact=True)
    usage_compact = build_usage_and_cost_section(stdin_data, metrics, model_id, compact=True)
    compact_parts = [native_compact, ctx_compact, usage_compact, speed_part]
    if quota_compact:
        compact_parts.append(quota_compact)
    compact_line = tight_sep.join(compact_parts)
    if visible_len(compact_line) <= term_width:
        return compact_line

    # Tier 3b: Tight single-line HUD for 90-108 column terminals
    ctx_tight = build_context_section(stdin_data, metrics, model_id, bar_width=5, compact=True)
    ctx_tight = ctx_tight.split("/")[0] + RESET
    cost_val = metrics.total_cost_usd + metrics.subagent_cost_usd
    usage_tight = (
        f"{FG_LABEL}Σ{RESET} {FG_TEXT}{BOLD}{format_tokens(metrics.total_tokens)}{RESET} "
        f"{FG_GOLD}{BOLD}{format_usd(cost_val)}{RESET}"
    )
    tight_parts = [native_compact, ctx_tight, usage_tight, speed_part]
    if quota_compact:
        tight_parts.append(quota_compact)
    tight_line = tight_sep.join(tight_parts)
    if visible_len(tight_line) <= term_width:
        return tight_line

    # Tier 4: Very narrow terminals (< 90 cols) -> clean 2-line stacked HUD
    line1_parts = [native_compact, speed_part]
    if quota_compact:
        line1_parts.append(quota_compact)
    line1 = tight_sep.join(line1_parts)
    line2 = f"{ctx_compact}{tight_sep}{usage_compact}"
    return f"{line1}\n{line2}"


# ---------------------------------------------------------------------------
# Settings Installer & CLI Diagnostics
# ---------------------------------------------------------------------------


CYCLE_ORDER = ("standard", "detailed", "minimal")
PRESET_WRAPPERS = {
    "简洁": "minimal",
    "标准": "standard",
    "详细": "detailed",
    "minimal": "minimal",
    "standard": "standard",
    "detailed": "detailed",
}


def detect_current_preset(settings_path: Path) -> str:
    """Detect the currently configured preset from `settings.json`."""
    if not settings_path.is_file():
        return "standard"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        cmd = str((data.get("statusLine") or {}).get("command") or "").strip()
        for token in reversed(cmd.split()):
            low = token.lower()
            if low in PRESET_ALIASES:
                return PRESET_ALIASES[low]
    except Exception:
        pass
    return "standard"


def ensure_standalone_in_settings(settings_path: Path) -> None:
    """Ensure `stack_with_default: false` is preserved when switched via `/statusline 简洁|标准|详细`."""
    if not settings_path.is_file():
        return
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        sl_cfg = data.get("statusLine")
        if isinstance(sl_cfg, dict) and "stack_with_default" not in sl_cfg:
            sl_cfg["stack_with_default"] = False
            settings_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    except Exception:
        pass


def install_preset_wrappers(
    bin_dir: Path, script_path: Path, uninstall: bool = False
) -> None:
    """Install or remove bilingual wrappers (`简洁/minimal`, `标准/standard`, `详细/detailed`) in `bin_dir`."""
    if not bin_dir.is_dir():
        return
    resolved_script = script_path.resolve().as_posix()
    # Always remove legacy agy-hud wrapper if present
    legacy = bin_dir / "agy-hud"
    if legacy.exists():
        try:
            legacy.unlink()
        except Exception:
            pass

    for cmd_name, preset_key in PRESET_WRAPPERS.items():
        target = bin_dir / cmd_name
        if uninstall:
            if target.exists():
                try:
                    target.unlink()
                except Exception:
                    pass
        else:
            try:
                target.write_text(
                    "#!/usr/bin/env bash\n"
                    f'SCRIPT="{resolved_script}"\n'
                    f'[ -f "$SCRIPT" ] && exec python3 "$SCRIPT" --preset {preset_key} "$@"\n'
                    "exit 0\n",
                    encoding="utf-8",
                )
                target.chmod(0o755)
            except Exception:
                pass


def configure_settings(
    settings_path: Path,
    script_path: Path,
    stack_with_default: bool = False,
    uninstall: bool = False,
    preset: str = "standard",
    cycle: bool = False,
    bin_dir: Path | None = None,
) -> dict[str, Any]:
    """Install, cycle, or uninstall the statusline command in `settings.json`."""
    data: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    if uninstall:
        data["statusLine"] = {
            "type": "",
            "command": "",
            "enabled": True,
        }
    else:
        if cycle:
            cur = detect_current_preset(settings_path)
            idx = CYCLE_ORDER.index(cur) if cur in CYCLE_ORDER else 0
            norm_p = CYCLE_ORDER[(idx + 1) % len(CYCLE_ORDER)]
        else:
            norm_p = normalize_preset(preset)

        cmd = f"python3 {script_path.resolve().as_posix()}"
        if norm_p != "standard":
            cmd += f" --preset {norm_p}"
        data["statusLine"] = {
            "type": "command",
            "command": cmd,
            "padding": 0,
            "enabled": True,
            "stack_with_default": bool(stack_with_default),
        }

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    default_settings = resolve_app_data_dir({}) / "settings.json"
    target_bin = bin_dir
    if target_bin is None and settings_path.resolve() == default_settings.resolve():
        target_bin = Path.home() / ".local" / "bin"
    if target_bin is not None:
        install_preset_wrappers(target_bin, script_path, uninstall=uninstall)

    return data


def run_hook_bootstrap(
    settings_path: Path,
    script_path: Path,
    bin_dir: Path | None = None,
) -> None:
    """Idempotent bootstrap triggered by `hooks.json` when installed via `/plugin install`.

    Automatically configures `settings.json` and installs `~/.local/bin` preset wrappers
    on first run without overwriting user-chosen presets or `/statusline off`.
    """
    default_settings = resolve_app_data_dir({}) / "settings.json"
    target_bin = bin_dir
    if target_bin is None and settings_path.resolve() == default_settings.resolve():
        target_bin = Path.home() / ".local" / "bin"

    needs_wrappers = False
    if target_bin is not None and target_bin.is_dir():
        for name in PRESET_WRAPPERS:
            w_path = target_bin / name
            if not w_path.is_file():
                needs_wrappers = True
                break
            try:
                txt = w_path.read_text(encoding="utf-8")
                m = re.search(r'SCRIPT="([^"]+)"', txt)
                if not m or not Path(m.group(1)).is_file():
                    needs_wrappers = True
                    break
            except Exception:
                needs_wrappers = True
                break

    already_configured = False
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            sl_cfg = data.get("statusLine")
            if isinstance(sl_cfg, dict):
                if sl_cfg.get("enabled") is False:
                    # User explicitly ran `/statusline off`; respect their choice
                    already_configured = True
                else:
                    cmd = str(sl_cfg.get("command") or "").strip()
                    if cmd in PRESET_WRAPPERS:
                        already_configured = True
                    elif "agy_statusline.py" in cmd:
                        # Verify the referenced script path still exists
                        parts = cmd.split()
                        py_paths = [p for p in parts if p.endswith("agy_statusline.py")]
                        if py_paths and Path(py_paths[0]).is_file():
                            already_configured = True
        except Exception:
            pass

    if not already_configured:
        configure_settings(
            settings_path=settings_path,
            script_path=script_path,
            preset=detect_current_preset(settings_path),
            bin_dir=target_bin,
        )
    else:
        ensure_standalone_in_settings(settings_path)
        if needs_wrappers and target_bin is not None:
            install_preset_wrappers(target_bin, script_path, uninstall=False)


def run_demo(app_data_dir: Path) -> None:
    """Print live and synthetic HUD previews across all 3 presets to stdout."""
    print(f"{BOLD}=== Antigravity Unified HUD Statusline Preview (3 Presets) ==={RESET}\n")

    conv_dir = app_data_dir / "conversations"
    latest_db: Path | None = None
    if conv_dir.is_dir():
        dbs = sorted(conv_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if dbs:
            latest_db = dbs[0]

    conv_id = latest_db.stem if latest_db is not None else ""
    sample_payload = {
        "conversation_id": conv_id,
        "model": {
            "id": "gemini-4-argon-high",
            "display_name": "Gemini 4 Argon",
            "effort": "High",
        },
        "cycle_mode": "auto",
        "vcs": {"type": "git", "branch": "main", "dirty": True},
        "context_window": {
            "total_input_tokens": 245_139,
            "total_output_tokens": 145_129,
            "context_window_size": 1_048_576,
            "current_usage": {
                "input_tokens": 7_176,
                "output_tokens": 1_137,
                "cache_read_input_tokens": 272_892,
            },
        },
        "quota": {
            "gemini-5h": {
                "remaining_fraction": 0.771,
                "reset_in_seconds": 1799,
            },
            "gemini-weekly": {
                "remaining_fraction": 0.887,
                "reset_in_seconds": 570599,
            },
            "3p-5h": {
                "remaining_fraction": 1.0,
                "reset_in_seconds": 16379,
            },
            "3p-weekly": {
                "remaining_fraction": 1.0,
                "reset_in_seconds": 603179,
            },
        },
        "agent_state": "working",
        "terminal_width": 176,
    }

    for preset_key, label in [
        ("minimal", "1. 简洁模式 (--preset minimal)"),
        ("standard", "2. 标准模式 (--preset standard, 默认)"),
        ("detailed", "3. 详细模式 (--preset detailed, 含 5h & Weekly Quota)"),
    ]:
        print(f"{FG_LABEL}{label}:{RESET}")
        print(
            render_statusline(
                sample_payload,
                app_data_dir=app_data_dir,
                use_cache=False,
                preset=preset_key,
            )
        )
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Antigravity CLI (agy) HUD Statusline Plugin"
    )
    parser.add_argument(
        "preset_pos",
        nargs="?",
        default=None,
        help="Optional positional preset: minimal (简洁), standard (标准), detailed (详细)",
    )
    parser.add_argument(
        "--preset",
        "-p",
        type=str,
        default=None,
        help="Display preset: minimal (简洁), standard (标准, default), detailed (详细, with 5h/weekly quota)",
    )
    parser.add_argument(
        "--cycle",
        action="store_true",
        help="Cycle to the next preset (standard -> detailed -> minimal) in settings.json",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install this statusline into ~/.gemini/antigravity-cli/settings.json (replaces default statusline)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove custom statusline from ~/.gemini/antigravity-cli/settings.json",
    )
    parser.add_argument(
        "--hook-bootstrap",
        action="store_true",
        help="Idempotent lifecycle hook bootstrap for `/plugin install` (outputs `{}` JSON)",
    )
    parser.add_argument(
        "--stack",
        action="store_true",
        help="When used with --install, set stack_with_default=true to stack alongside the built-in statusline",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="When used with --install, set stack_with_default=false (default)",
    )
    parser.add_argument(
        "--settings-path",
        type=Path,
        default=None,
        help="Custom path to settings.json (defaults to ~/.gemini/antigravity-cli/settings.json)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Render a preview of the HUD statusline across all 3 presets",
    )
    args = parser.parse_args(argv)

    if args.hook_bootstrap:
        try:
            raw_hook = sys.stdin.read().strip()
            hook_data = json.loads(raw_hook) if raw_hook else {}
            if not isinstance(hook_data, dict):
                hook_data = {}
        except Exception:
            hook_data = {}
        app_data_dir = resolve_app_data_dir(hook_data)
        settings_path = args.settings_path or (app_data_dir / "settings.json")
        try:
            run_hook_bootstrap(settings_path=settings_path, script_path=Path(__file__))
        except Exception:
            pass
        sys.stdout.write("{}\n")
        sys.stdout.flush()
        return 0

    app_data_dir = resolve_app_data_dir({})
    settings_path = args.settings_path or (app_data_dir / "settings.json")
    effective_preset = args.preset or args.preset_pos

    if args.install or args.uninstall or args.cycle:
        updated = configure_settings(
            settings_path=settings_path,
            script_path=Path(__file__),
            stack_with_default=bool(args.stack and not args.standalone),
            uninstall=args.uninstall,
            preset=effective_preset or "standard",
            cycle=args.cycle,
        )
        print(json.dumps(updated.get("statusLine", {}), indent=2))
        return 0

    if args.demo:
        run_demo(app_data_dir)
        return 0

    # Standard statusline execution mode: read StatusLineData JSON from stdin
    try:
        raw = sys.stdin.read().strip()
        stdin_data = json.loads(raw) if raw else {}
        if not isinstance(stdin_data, dict):
            stdin_data = {}
    except Exception:
        stdin_data = {}

    ensure_standalone_in_settings(settings_path)

    try:
        output = render_statusline(stdin_data, preset=effective_preset)
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
    except Exception:
        # Never exit non-zero so agy StatusLineRunner never auto-disables the statusline
        sys.stdout.write(f"{FG_LABEL}CTX{RESET} [░░░░░░░░░░] 0.0%  │  Σ 0  │  ⚡ -- tok/s\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
