#!/usr/bin/env python3
"""Unit and integration tests for `agy_statusline`."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agy_statusline as sl


# ---------------------------------------------------------------------------
# Protobuf Wire Helpers for Test Fixtures
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    val = int(value)
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _encode_tag(field_num: int, wire_type: int) -> bytes:
    return _encode_varint((field_num << 3) | wire_type)


def _encode_varint_field(field_num: int, value: int) -> bytes:
    return _encode_tag(field_num, 0) + _encode_varint(value)


def _encode_bytes_field(field_num: int, payload: bytes) -> bytes:
    return _encode_tag(field_num, 2) + _encode_varint(len(payload)) + payload


def _encode_duration(seconds: int, nanos: int = 0) -> bytes:
    body = b""
    if seconds:
        body += _encode_varint_field(1, seconds)
    if nanos:
        body += _encode_varint_field(2, nanos)
    return body


def build_synthetic_gen_metadata_blob(
    model_name: str = "gemini-4-argon",
    input_tokens: int = 4000,
    output_tokens: int = 1200,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 96000,
    thinking_tokens: int = 400,
    response_tokens: int = 800,
    estimated_ctx: int = 100000,
    max_ctx: int = 256000,
    ttft_sec: int = 5,
    ttft_nanos: int = 500_000_000,
    stream_sec: int = 14,
    stream_nanos: int = 500_000_000,
) -> bytes:
    """Construct a valid `CortexStepGeneratorMetadata` protobuf wire blob."""
    usage_msg = (
        _encode_varint_field(2, input_tokens)
        + _encode_varint_field(3, output_tokens)
        + _encode_varint_field(4, cache_write_tokens)
        + _encode_varint_field(5, cache_read_tokens)
        + _encode_varint_field(9, thinking_tokens)
        + _encode_varint_field(10, response_tokens)
    )
    ctx_win_msg = _encode_varint_field(1, estimated_ctx) + _encode_varint_field(
        4, max_ctx
    )
    chat_start_msg = _encode_bytes_field(10, ctx_win_msg)
    ttft_msg = _encode_duration(ttft_sec, ttft_nanos)
    stream_msg = _encode_duration(stream_sec, stream_nanos)

    chat_model_msg = (
        _encode_bytes_field(4, usage_msg)
        + _encode_bytes_field(9, chat_start_msg)
        + _encode_bytes_field(11, ttft_msg)
        + _encode_bytes_field(12, stream_msg)
        + _encode_bytes_field(19, model_name.encode("utf-8"))
    )
    return _encode_bytes_field(1, chat_model_msg)


class TestProtobufParser(unittest.TestCase):
    def test_parse_valid_gen_metadata_blob(self):
        blob = build_synthetic_gen_metadata_blob(
            model_name="gemini-4-argon",
            input_tokens=3500,
            output_tokens=1500,
            cache_write_tokens=1000,
            cache_read_tokens=120000,
            thinking_tokens=500,
            response_tokens=1000,
            estimated_ctx=124500,
            max_ctx=256000,
            ttft_sec=4,
            ttft_nanos=250_000_000,
            stream_sec=10,
            stream_nanos=750_000_000,
        )
        turn = sl.parse_gen_metadata_blob(blob, idx=7)
        self.assertEqual(turn.idx, 7)
        self.assertEqual(turn.model_name, "gemini-4-argon")
        self.assertEqual(turn.input_tokens, 3500)
        self.assertEqual(turn.output_tokens, 1500)
        self.assertEqual(turn.cache_write_tokens, 1000)
        self.assertEqual(turn.cache_read_tokens, 120000)
        self.assertEqual(turn.thinking_output_tokens, 500)
        self.assertEqual(turn.response_output_tokens, 1000)
        self.assertEqual(turn.estimated_context_tokens, 124500)
        self.assertEqual(turn.max_context_tokens, 256000)
        self.assertAlmostEqual(turn.ttft_seconds, 4.25, places=4)
        self.assertAlmostEqual(turn.streaming_seconds, 10.75, places=4)

    def test_parse_empty_or_corrupt_blob_never_raises(self):
        empty_turn = sl.parse_gen_metadata_blob(b"", idx=1)
        self.assertEqual(empty_turn.input_tokens, 0)
        self.assertEqual(empty_turn.output_tokens, 0)

        # Truncated / malformed bytes
        for bad in (b"\x0a\xff\xff\xff", b"\x08\x80", b"\x0a\x10abc"):
            t = sl.parse_gen_metadata_blob(bad, idx=2)
            self.assertEqual(t.idx, 2)


class TestPricingAndFormatting(unittest.TestCase):
    def test_model_pricing_tiers(self):
        # Gemini Pro <= 200k vs > 200k
        pro = sl.get_model_pricing("gemini-4-argon-high")
        cost_low_ctx = pro.compute_turn_cost(
            input_tokens=100_000,
            output_tokens=10_000,
            cache_write_tokens=0,
            cache_read_tokens=0,
        )
        # 0.1M * $1.25 + 0.01M * $10.00 = 0.125 + 0.10 = $0.225
        self.assertAlmostEqual(cost_low_ctx, 0.225, places=6)

        cost_high_ctx = pro.compute_turn_cost(
            input_tokens=50_000,
            output_tokens=10_000,
            cache_write_tokens=0,
            cache_read_tokens=200_000,  # total ctx = 250k > 200k
        )
        # 0.05M * $2.50 + 0.01M * $15.00 + 0.20M * $0.625 = 0.125 + 0.15 + 0.125 = $0.40
        self.assertAlmostEqual(cost_high_ctx, 0.40, places=6)

        # Claude Sonnet
        sonnet = sl.get_model_pricing("claude-sonnet-5-5")
        cost_sonnet = sonnet.compute_turn_cost(
            input_tokens=100_000,
            output_tokens=20_000,
            cache_write_tokens=0,
            cache_read_tokens=500_000,
        )
        # 0.1M * 3.0 + 0.02M * 15.0 + 0.5M * 0.30 = 0.30 + 0.30 + 0.15 = $0.75
        self.assertAlmostEqual(cost_sonnet, 0.75, places=6)

        # Gemini Flash
        flash = sl.get_model_pricing("gemini-3.8-flash-medium")
        cost_flash = flash.compute_turn_cost(1_000_000, 100_000, 0, 0)
        self.assertAlmostEqual(cost_flash, 0.30 + 0.25, places=6)

    def test_hud_bar_rendering(self):
        bar_zero = sl.strip_ansi(sl.render_hud_bar(0.0, width=10))
        self.assertEqual(bar_zero, "[░░░░░░░░░░]")

        bar_half = sl.strip_ansi(sl.render_hud_bar(50.0, width=10))
        self.assertEqual(bar_half, "[█████░░░░░]")

        bar_full = sl.strip_ansi(sl.render_hud_bar(100.0, width=10))
        self.assertEqual(bar_full, "[██████████]")

        bar_over = sl.strip_ansi(sl.render_hud_bar(135.0, width=10))
        self.assertEqual(bar_over, "[██████████]")


class TestDatabaseAndSessionAggregation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.app_dir = Path(self.tmp_dir.name) / "agy-data"
        self.conv_dir = self.app_dir / "conversations"
        self.conv_dir.mkdir(parents=True)
        self.cache_dir = Path(self.tmp_dir.name) / "cache"
        self.cache_dir.mkdir(parents=True)
        self._old_cache_env = os.environ.get("AGY_STATUSLINE_CACHE_DIR")
        os.environ["AGY_STATUSLINE_CACHE_DIR"] = str(self.cache_dir)

    def tearDown(self):
        if self._old_cache_env is None:
            os.environ.pop("AGY_STATUSLINE_CACHE_DIR", None)
        else:
            os.environ["AGY_STATUSLINE_CACHE_DIR"] = self._old_cache_env
        self.tmp_dir.cleanup()

    def _create_conv_db(self, conv_id: str, turns: list[tuple[int, bytes]]) -> Path:
        db_path = self.conv_dir / f"{conv_id}.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)"
            )
            for idx, blob in turns:
                conn.execute(
                    "INSERT OR REPLACE INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                    (idx, blob, len(blob)),
                )
            conn.commit()
        return db_path

    def test_incremental_caching_and_subagent_aggregation(self):
        conv_id = "test-session-001"
        sub_id = "subagent-session-002"

        t1 = build_synthetic_gen_metadata_blob(
            input_tokens=10_000,
            output_tokens=1_000,
            cache_read_tokens=40_000,
            thinking_tokens=300,
            response_tokens=700,
            estimated_ctx=50_000,
            max_ctx=256_000,
            ttft_sec=4,
            ttft_nanos=0,
            stream_sec=16,
            stream_nanos=0,
        )
        self._create_conv_db(conv_id, [(1, t1)])

        m1 = sl.load_session_metrics(conv_id, self.app_dir, use_cache=True)
        self.assertEqual(m1.turns_count, 1)
        self.assertEqual(m1.input_tokens, 10_000)
        self.assertEqual(m1.cache_read_tokens, 40_000)
        self.assertEqual(m1.output_tokens, 1_000)
        self.assertAlmostEqual(m1.last_turn_tps, 1000 / 20.0, places=2)  # 50.0 tok/s

        # Append turn 2 and verify incremental load picks up only idx > 1
        t2 = build_synthetic_gen_metadata_blob(
            input_tokens=5_000,
            output_tokens=2_000,
            cache_read_tokens=50_000,
            thinking_tokens=500,
            response_tokens=1_500,
            estimated_ctx=60_000,
            max_ctx=256_000,
            ttft_sec=10,
            ttft_nanos=0,
            stream_sec=30,
            stream_nanos=0,
        )
        self._create_conv_db(conv_id, [(2, t2)])

        # Also add a child subagent linked via conversation_summaries.db
        sub_blob = build_synthetic_gen_metadata_blob(
            model_name="gemini-3.8-flash",
            input_tokens=20_000,
            output_tokens=2_000,
            cache_read_tokens=0,
            estimated_ctx=20_000,
            max_ctx=256_000,
        )
        self._create_conv_db(sub_id, [(1, sub_blob)])
        with sqlite3.connect(self.app_dir / "conversation_summaries.db") as conn:
            conn.execute(
                "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, parent_conversation_id TEXT)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?)", (sub_id, conv_id)
            )
            conn.commit()

        m2 = sl.load_session_metrics(conv_id, self.app_dir, use_cache=True)
        self.assertEqual(m2.turns_count, 2)
        self.assertEqual(m2.input_tokens, 15_000)
        self.assertEqual(m2.cache_read_tokens, 90_000)
        self.assertEqual(m2.output_tokens, 3_000)
        self.assertEqual(m2.subagent_tokens, 22_000)
        self.assertGreater(m2.subagent_cost_usd, 0.0)
        # Avg session speed: (1000 + 2000) / (20 + 40) = 3000 / 60 = 50.0 tok/s
        self.assertAlmostEqual(m2.avg_session_tps, 50.0, places=2)

    def test_native_and_slimmed_sections(self):
        conv_id = "test-slim-session"
        t1 = build_synthetic_gen_metadata_blob(
            input_tokens=10_000,
            output_tokens=1_500,
            cache_read_tokens=90_000,
            thinking_tokens=500,
            response_tokens=1_000,
            estimated_ctx=100_000,
            max_ctx=256_000,
            ttft_sec=5,
            ttft_nanos=0,
            stream_sec=25,
            stream_nanos=0,
        )
        self._create_conv_db(conv_id, [(1, t1)])

        out = sl.render_statusline(
            {
                "conversation_id": conv_id,
                "model": {
                    "id": "gemini-4-argon-high",
                    "display_name": "Gemini 4 Argon",
                    "effort": "High",
                },
                "cycle_mode": "accept-edits",
                "vcs": {"type": "git", "branch": "feat/hud", "dirty": True},
                "subagents": [{"name": "research", "status": "running"}],
                "task_count": 2,
                "vim": {"mode": "INSERT"},
                "agent_state": "working",
                "terminal_width": 180,
            },
            app_data_dir=self.app_dir,
        )
        plain = sl.strip_ansi(out)
        # Native info integrated
        self.assertIn("-- INSERT --", plain)
        self.assertIn("Gemini 4 Argon (High)", plain)
        self.assertIn("accept-edits", plain)
        self.assertIn("⎇ feat/hud*", plain)
        self.assertIn("1 subagent", plain)
        self.assertIn("2 tasks", plain)
        # Slimmed token usage: total + cache hit rate only (no In: / Out: / Th:)
        self.assertIn("Σ 102k (Cache 90%)", plain)
        self.assertNotIn("In:", plain)
        self.assertNotIn("Out:", plain)
        self.assertNotIn("Th:", plain)
        # Slimmed speed: last turn tok/s only (no avg / strm / TTFT)
        self.assertIn("⚡ 50.0 tok/s", plain)
        self.assertNotIn("avg", plain)
        self.assertNotIn("strm", plain)
        self.assertNotIn("TTFT", plain)

    def test_responsive_terminal_widths(self):
        conv_id = "test-width-session"
        t1 = build_synthetic_gen_metadata_blob(
            input_tokens=12_000,
            output_tokens=1_500,
            cache_read_tokens=180_000,
            thinking_tokens=400,
            response_tokens=1_100,
            estimated_ctx=192_000,
            max_ctx=256_000,
            ttft_sec=6,
            ttft_nanos=0,
            stream_sec=24,
            stream_nanos=0,
        )
        self._create_conv_db(conv_id, [(1, t1)])

        for width in (160, 120, 95):
            out = sl.render_statusline(
                {
                    "conversation_id": conv_id,
                    "model": {
                        "id": "gemini-4-argon-high",
                        "display_name": "Gemini 4 Argon",
                        "effort": "High",
                    },
                    "cycle_mode": "accept-edits",
                    "agent_state": "working",
                    "terminal_width": width,
                },
                app_data_dir=self.app_dir,
            )
            self.assertNotIn("\n", out)
            self.assertLessEqual(sl.visible_len(out), width)
            plain = sl.strip_ansi(out)
            self.assertIn("Gemini 4 Argon", plain)
            self.assertIn("CTX", plain)
            self.assertIn("75", plain)
            self.assertIn("tok/s", plain)

        # Narrow terminal (<85 cols) should produce 2-line stacked output
        narrow_out = sl.render_statusline(
            {
                "conversation_id": conv_id,
                "model": {
                    "id": "gemini-4-argon-high",
                    "display_name": "Gemini 4 Argon",
                    "effort": "High",
                },
                "cycle_mode": "accept-edits",
                "terminal_width": 70,
            },
            app_data_dir=self.app_dir,
        )
        self.assertEqual(len(narrow_out.splitlines()), 2)

    def test_presets_and_quota_rendering(self):
        conv_id = "test-preset-session"
        t1 = build_synthetic_gen_metadata_blob(
            input_tokens=10_000,
            output_tokens=1_500,
            cache_read_tokens=90_000,
            thinking_tokens=500,
            response_tokens=1_000,
            estimated_ctx=100_000,
            max_ctx=256_000,
            ttft_sec=5,
            ttft_nanos=0,
            stream_sec=25,
            stream_nanos=0,
        )
        self._create_conv_db(conv_id, [(1, t1)])

        sample_data = {
            "conversation_id": conv_id,
            "model": {
                "id": "gemini-4-argon-high",
                "display_name": "Gemini 4 Argon",
                "effort": "High",
            },
            "cycle_mode": "accept-edits",
            "vcs": {"type": "git", "branch": "main", "dirty": True},
            "quota": {
                "3p-5h": {
                    "remaining_fraction": 1.0,
                    "reset_in_seconds": 16379,
                },
                "3p-weekly": {
                    "remaining_fraction": 0.95,
                    "reset_in_seconds": 603179,
                },
                "gemini-5h": {
                    "remaining_fraction": 0.77128,
                    "reset_in_seconds": 1799,
                },
                "gemini-weekly": {
                    "remaining_fraction": 0.88654,
                    "reset_in_seconds": 570599,
                },
            },
            "terminal_width": 200,
        }

        # 1. minimal preset (简洁): compact CTX %, no (100k/256k), no Cache %, no Quota
        out_min = sl.strip_ansi(
            sl.render_statusline(sample_data, app_data_dir=self.app_dir, preset="minimal")
        )
        self.assertIn("Gemini 4 Argon (H) · accept-edits", out_min)
        self.assertNotIn("⎇ main*", out_min)
        self.assertIn("39%", out_min)
        self.assertNotIn("(100k/256k)", out_min)
        self.assertIn("Σ 102k · 💰", out_min)
        self.assertNotIn("Cache", out_min)
        self.assertNotIn("5h:", out_min)

        # 2. standard preset (标准): includes git, (100k/256k), Cache %, but no Quota
        out_std = sl.strip_ansi(
            sl.render_statusline(sample_data, app_data_dir=self.app_dir, preset="standard")
        )
        self.assertIn("⎇ main*", out_std)
        self.assertIn("(100k/256k)", out_std)
        self.assertIn("Σ 102k (Cache 90%)", out_std)
        self.assertNotIn("5h:", out_std)

        # 3. detailed preset (详细): includes everything in standard + 5h & weekly quota
        out_det = sl.strip_ansi(
            sl.render_statusline(sample_data, app_data_dir=self.app_dir, preset="detailed")
        )
        self.assertIn("⎇ main*", out_det)
        self.assertIn("(100k/256k)", out_det)
        self.assertIn("Σ 102k (Cache 90%)", out_det)
        self.assertIn("Quota 5h:77%(29m) Wk:89%(6d14h)", out_det)

        # 4. Verify Chinese alias ("详细") and 3P model bucket selection (Claude -> 3p-5h / 3p-weekly)
        claude_data = dict(sample_data)
        claude_data["model"] = {
            "id": "claude-opus-5-5",
            "display_name": "Claude Opus 5.5",
            "effort": "High",
        }
        out_claude = sl.strip_ansi(
            sl.render_statusline(claude_data, app_data_dir=self.app_dir, preset="详细")
        )
        self.assertIn("Quota 5h:100%(4h32m) Wk:95%(6d23h)", out_claude)


class TestCLIIntegration(unittest.TestCase):
    def test_cli_stdin_and_install_flags(self):
        script = Path(__file__).resolve().parents[1] / "agy_statusline.py"
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "settings.json"
            settings_file.write_text('{"model": "Gemini 4 Argon"}\n', encoding="utf-8")

            # 1. Test --install (defaults to standalone: stack_with_default = False)
            subprocess.run(
                [sys.executable, str(script), "--install", "--settings-path", str(settings_file)],
                capture_output=True,
                text=True,
                check=True,
            )
            saved = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["statusLine"]["type"], "command")
            self.assertTrue(saved["statusLine"]["enabled"])
            self.assertFalse(saved["statusLine"]["stack_with_default"])
            self.assertNotIn("--preset", saved["statusLine"]["command"])

            # 1b. Test --install --preset detailed
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--install",
                    "--preset",
                    "detailed",
                    "--settings-path",
                    str(settings_file),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            saved_det = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertIn("--preset detailed", saved_det["statusLine"]["command"])

            # 2. Test stdin statusline execution (standard & detailed)
            sample_stdin = json.dumps(
                {
                    "conversation_id": "nonexistent-conv",
                    "model": {
                        "id": "gemini-4-argon-high",
                        "display_name": "Gemini 4 Argon",
                        "effort": "High",
                    },
                    "cycle_mode": "accept-edits",
                    "context_window": {
                        "total_input_tokens": 128000,
                        "total_output_tokens": 5000,
                        "context_window_size": 256000,
                        "current_usage": {
                            "input_tokens": 8000,
                            "output_tokens": 5000,
                            "cache_read_input_tokens": 120000,
                        },
                    },
                    "quota": {
                        "gemini-5h": {
                            "remaining_fraction": 0.77,
                            "reset_in_seconds": 1799,
                        },
                        "gemini-weekly": {
                            "remaining_fraction": 0.89,
                            "reset_in_seconds": 570599,
                        },
                    },
                    "terminal_width": 180,
                }
            )
            res_run = subprocess.run(
                [sys.executable, str(script)],
                input=sample_stdin,
                capture_output=True,
                text=True,
                check=True,
            )
            plain = sl.strip_ansi(res_run.stdout.strip())
            self.assertIn("Gemini 4 Argon (High) · accept-edits", plain)
            self.assertIn("CTX [█████░░░░░] 50.0% (128k/256k)", plain)
            self.assertIn("Σ 133k (Cache 94%)", plain)
            self.assertIn("$0.098", plain)
            self.assertNotIn("5h:77%", plain)

            res_det = subprocess.run(
                [sys.executable, str(script), "--preset", "detailed"],
                input=sample_stdin,
                capture_output=True,
                text=True,
                check=True,
            )
            plain_det = sl.strip_ansi(res_det.stdout.strip())
            self.assertIn("Quota 5h:77%(29m) Wk:89%(6d14h)", plain_det)

            # 2b. Test /statusline 简洁|minimal | 标准|standard | 详细|detailed wrappers & standalone preservation
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            (bin_dir / "agy-hud").write_text("#!/bin/sh\n", encoding="utf-8")
            sl.configure_settings(
                settings_path=settings_file,
                script_path=script,
                bin_dir=bin_dir,
            )
            self.assertFalse((bin_dir / "agy-hud").exists())
            all_wrappers = ("简洁", "标准", "详细", "minimal", "standard", "detailed")
            for wrapper_name in all_wrappers:
                self.assertTrue((bin_dir / wrapper_name).is_file())

            # Simulate agy's `/statusline 详细` overwriting statusLine without stack_with_default
            settings_file.write_text(
                json.dumps(
                    {
                        "statusLine": {
                            "type": "command",
                            "command": "详细",
                            "enabled": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            res_wrapper_det = subprocess.run(
                [str(bin_dir / "详细"), "--settings-path", str(settings_file)],
                input=sample_stdin,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("Quota 5h:77%(29m) Wk:89%(6d14h)", sl.strip_ansi(res_wrapper_det.stdout))
            repaired = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertFalse(repaired["statusLine"]["stack_with_default"])

            # Verify English wrapper (`minimal`) produces identical preset output to `简洁`
            res_wrapper_min = subprocess.run(
                [str(bin_dir / "minimal"), "--settings-path", str(settings_file)],
                input=sample_stdin,
                capture_output=True,
                text=True,
                check=True,
            )
            plain_wrapper_min = sl.strip_ansi(res_wrapper_min.stdout)
            self.assertNotIn("(128k/256k)", plain_wrapper_min)
            self.assertNotIn("Cache", plain_wrapper_min)

            # 2c. Test --cycle (detailed -> minimal -> standard -> detailed)
            subprocess.run(
                [sys.executable, str(script), "--cycle", "--settings-path", str(settings_file)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(sl.detect_current_preset(settings_file), "minimal")

            subprocess.run(
                [sys.executable, str(script), "--cycle", "--settings-path", str(settings_file)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(sl.detect_current_preset(settings_file), "standard")

            subprocess.run(
                [sys.executable, str(script), "--cycle", "--settings-path", str(settings_file)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(sl.detect_current_preset(settings_file), "detailed")

            # 2d. Test uninstall removes all bilingual wrappers
            sl.configure_settings(
                settings_path=settings_file,
                script_path=script,
                uninstall=True,
                bin_dir=bin_dir,
            )
            for wrapper_name in all_wrappers:
                self.assertFalse((bin_dir / wrapper_name).exists())

            # 2e. Test --hook-bootstrap auto-configures unconfigured settings and respects /statusline off
            sl.run_hook_bootstrap(
                settings_path=settings_file,
                script_path=script,
                bin_dir=bin_dir,
            )
            boot_cfg = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertTrue(boot_cfg["statusLine"]["enabled"])
            self.assertFalse(boot_cfg["statusLine"]["stack_with_default"])
            for wrapper_name in all_wrappers:
                self.assertTrue((bin_dir / wrapper_name).is_file())

            # Simulate user running `/statusline off` and verify --hook-bootstrap respects it
            settings_file.write_text(
                json.dumps({"statusLine": {"type": "command", "command": "标准", "enabled": False}}),
                encoding="utf-8",
            )
            res_hook = subprocess.run(
                [sys.executable, str(script), "--hook-bootstrap", "--settings-path", str(settings_file)],
                input='{"conversationId": "c1"}',
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res_hook.stdout.strip(), "{}")
            after_off = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertFalse(after_off["statusLine"]["enabled"])

            # 3. Test malformed stdin never exits non-zero
            res_bad = subprocess.run(
                [sys.executable, str(script)],
                input="{not valid json",
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res_bad.returncode, 0)
            self.assertIn("CTX", res_bad.stdout)


if __name__ == "__main__":
    unittest.main()
