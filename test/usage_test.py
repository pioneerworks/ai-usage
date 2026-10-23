#!/usr/bin/env python3
"""Run the report against isolated histories and a fixture-only HTTP boundary."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "usage.sh"
DAY = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
STAMP = DAY + "T12:00:00Z"
CATALOG = {"data": [
    {"id": "anthropic/claude-sonnet-4.6", "pricing": {
        "input": "0.000003", "output": "0.000015",
        "input_cache_read": "0.0000003", "input_cache_write": "0.00000375"}},
    {"id": "anthropic/claude-opus-4.6", "pricing": {
        "input": "0.000005", "output": "0.000025",
        "input_cache_read": "0.0000005", "input_cache_write": "0.00000625"}},
    {"id": "openai/gpt-5", "pricing": {
        "input": "0.000001", "output": "0.000002", "input_cache_read": "0.0000001"}},
    {"id": "deepseek/deepseek-v4-pro", "pricing": {
        "input": "0.000001", "output": "0.000002", "input_cache_read": "0.0000001"}},
]}

# Network is replaced at the HTTP boundary; parsing, accounting, caching, and output run normally.
HTTP_BOUNDARY = '''
import io, json, os, threading, urllib.error, urllib.parse, urllib.request
from pathlib import Path
_fixture = json.loads(Path(os.environ["USAGE_TEST_FIXTURE"]).read_text())
_lock = threading.Lock()
def fixture_urlopen(request, *args, **kwargs):
    url = request.full_url if hasattr(request, "full_url") else request
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    headers = {key.lower(): value for key, value in request.header_items()} if hasattr(request, "header_items") else {}
    auth = headers.get("authorization")
    expected = os.environ.get("USAGE_TEST_TOKEN")
    native_key = headers.get("x-api-key")
    native_expected = os.environ.get("USAGE_TEST_NATIVE_TOKEN")
    openai_expected = os.environ.get("USAGE_TEST_OPENAI_TOKEN")
    record = {"host": parsed.hostname, "path": parsed.path, "query": query,
              "has_auth": bool(auth), "expected_auth": bool(expected and auth == "Bearer " + expected),
              "has_native_key": bool(native_key),
              "expected_native_key": bool(native_expected and native_key == native_expected),
              "expected_openai_key": bool(openai_expected and auth == "Bearer " + openai_expected),
              "anthropic_version": headers.get("anthropic-version")}
    with _lock, open(os.environ["USAGE_TEST_CALLS"], "a") as calls:
        calls.write(json.dumps(record) + "\\n")
    if parsed.hostname == "api.anthropic.com":
        if parsed.path != "/v1/organizations/analytics/user_cost_report":
            raise AssertionError("Unexpected Anthropic endpoint")
        if (_fixture.get("native_error") or
                (native_expected and not record["expected_native_key"])):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if _fixture.get("native_redirect"):
            raise urllib.error.HTTPError(url, 302, "Redirect", {"Location": "https://elsewhere.invalid"}, None)
        window = query.get("starting_at", [""])[0]
        page = query.get("page", ["first"])[0]
        data = (_fixture.get("native_windows", {}).get(window) or
                _fixture.get("native_pages", {}).get(page) or _fixture["native_response"])
        return io.BytesIO(json.dumps(data).encode())
    if parsed.hostname == "api.openai.com":
        if parsed.path != "/v1/organization/costs":
            raise AssertionError("Unexpected OpenAI endpoint")
        if _fixture.get("openai_error") or (openai_expected and not record["expected_openai_key"]):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if _fixture.get("openai_redirect"):
            raise urllib.error.HTTPError(url, 302, "Redirect", {"Location": "https://elsewhere.invalid"}, None)
        page = query.get("page", ["first"])[0]
        data = _fixture.get("openai_pages", {}).get(page) or _fixture["openai_response"]
        return io.BytesIO(json.dumps(data).encode())
    if parsed.hostname != "ai-gateway.vercel.sh":
        raise AssertionError("Unexpected destination")
    if parsed.path == "/v1/models":
        if _fixture.get("catalog_error"):
            raise urllib.error.URLError("catalog unavailable")
        data = _fixture["catalog"]
    elif parsed.path == "/v1/report":
        if _fixture.get("gateway_redirect"):
            raise urllib.error.HTTPError(url, 302, "Redirect", {"Location": "https://elsewhere.invalid"}, None)
        if (_fixture.get("report_error") or
                (_fixture.get("model_error") and query.get("group_by") == ["model"]) or
                (expected and not record["expected_auth"])):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if query.get("api_key_id") != ["self"]:
            data = {"results": _fixture.get("team_rows", [])}
        elif query.get("group_by") == ["model"]:
            data = {"results": _fixture["model_rows"]}
        else:
            data = {"results": _fixture["daily_rows"]}
    elif parsed.path == "/v1/generation":
        generation = _fixture.get("generations", {}).get(query.get("id", [""])[0])
        if generation is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        data = {"data": generation}
    else:
        raise AssertionError("Unexpected endpoint: " + parsed.path)
    return io.BytesIO(json.dumps(data).encode())
urllib.request.urlopen = fixture_urlopen
def fixture_opener_open(self, request, *args, **kwargs):
    host = urllib.parse.urlparse(request.full_url).hostname
    if ((_fixture.get("native_redirect") and host == "api.anthropic.com") or
            (_fixture.get("openai_redirect") and host == "api.openai.com") or
            (_fixture.get("gateway_redirect") and host == "ai-gateway.vercel.sh")):
        handler = next((handler for handler in self.handlers
                        if isinstance(handler, urllib.request.HTTPRedirectHandler)), None)
        if handler is None or handler.redirect_request(request, None, 302, "Redirect", {},
                                                       "https://elsewhere.invalid") is not None:
            raise AssertionError("Native credential would follow redirect")
    return fixture_urlopen(request, *args, **kwargs)
urllib.request.OpenerDirector.open = fixture_opener_open
'''


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="usage-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.claude = self.home / "claude-config"
        self.codex = self.home / "codex-config"
        self.state = self.home / "state"
        self.fixture_path = self.home / "fixture.json"
        self.calls_path = self.home / "calls.jsonl"
        self.boundary = self.home / "boundary"
        self.boundary.mkdir()
        (self.boundary / "sitecustomize.py").write_text(HTTP_BOUNDARY)
        bindir = self.home / "bin"
        bindir.mkdir()
        security = bindir / "security"
        security.write_text("#!/bin/sh\nexit 44\n")
        security.chmod(0o700)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ANTHROPIC_", "CLAUDE_", "AI_GATEWAY_", "VERCEL_",
                                           "OPENAI_", "USAGE_TEST_"))}
        self.env.update({"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.claude),
                         "CODEX_HOME": str(self.codex), "XDG_STATE_HOME": str(self.state),
                         "NO_COLOR": "1", "TZ": "UTC", "COLUMNS": "99", "PYTHONPATH": str(self.boundary),
                         "PYTHONDONTWRITEBYTECODE": "1", "USAGE_TEST_FIXTURE": str(self.fixture_path),
                         "USAGE_TEST_CALLS": str(self.calls_path)})
        self.env["PATH"] = str(bindir) + os.pathsep + self.env.get("PATH", "")
        self.fixture = {"catalog": CATALOG,
                        "native_response": {
                            "data": [{"actor": {"type": "user_actor", "user_id": "user_fixture",
                                                "email_address": "owner@example.com"},
                                      "amount": "125.500000", "list_amount": "999.000000",
                                      "currency": "USD", "starting_at": DAY + "T00:00:00Z",
                                      "ending_at": (datetime.fromisoformat(DAY) + timedelta(days=1)).date().isoformat() + "T00:00:00Z"}],
                            "data_refreshed_at": datetime.now(timezone.utc).isoformat(),
                            "has_more": False, "next_page": None,
                            "organization_id": "org_fixture"},
                        "openai_response": {
                            "object": "page", "data": [{
                                "object": "bucket",
                                "start_time": int(datetime.fromisoformat(DAY).replace(
                                    tzinfo=timezone.utc).timestamp()),
                                "end_time": int((datetime.fromisoformat(DAY).replace(
                                    tzinfo=timezone.utc) + timedelta(days=1)).timestamp()),
                                "results": [{"object": "organization.costs.result",
                                             "api_key_id": "key_fixture",
                                             "amount": {"value": 0.75, "currency": "usd"}}]}],
                            "has_more": False, "next_page": None},
                        "daily_rows": [{"day": DAY, "api_key_name": "Another Person",
                                        "total_cost": 2.5, "request_count": 3}],
                        "team_rows": [],
                        "model_rows": [
                            {"model": "deepseek/deepseek-v4-pro", "total_cost": 0.5, "request_count": 1,
                             "input_tokens": 1000000, "output_tokens": 100000,
                             "cached_input_tokens": 0, "cache_creation_input_tokens": 0},
                            {"model": "anthropic/claude-sonnet-4.6", "total_cost": 2.0, "request_count": 2,
                             "input_tokens": 500000, "output_tokens": 0,
                             "cached_input_tokens": 500000, "cache_creation_input_tokens": 0}],
                        "generations": {"gen_example": {
                            "model": "deepseek/deepseek-v4-pro", "total_cost": 0.5, "created_at": STAMP}}}

    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def assistant(self, message_id="msg_example", model="claude-sonnet-4-6", **usage):
        return {"type": "assistant", "timestamp": STAMP, "uuid": message_id,
                "message": {"id": message_id, "model": model, "usage": usage}}

    def claude_history(self):
        path = self.claude / "projects/example/session.jsonl"
        self.write_jsonl(path, [
            self.assistant(input_tokens=0, output_tokens=0),
            self.assistant(input_tokens=1000000, output_tokens=100000, cache_read_input_tokens=1000000)])
        return path

    def codex_history(self, provider="openai", folder="sessions"):
        info = {"total_token_usage": {"input_tokens": 1000000, "output_tokens": 100000},
                "last_token_usage": {"input_tokens": 1000000, "cached_input_tokens": 200000,
                                     "output_tokens": 100000}}
        rows = [{"type": "session_meta", "payload": {"model_provider": provider}},
                {"type": "turn_context", "payload": {"model": "gpt-5"}},
                {"type": "event_msg", "timestamp": STAMP,
                 "payload": {"type": "token_count", "info": info}}]
        self.write_jsonl(self.codex / folder / "rollout-example.jsonl", rows + rows[-1:])

    def gateway_key(self, value="fixture-gateway-secret"):
        self.env["AI_GATEWAY_API_KEY"] = value
        self.env["USAGE_TEST_TOKEN"] = value

    def native_identity(self):
        self.claude.mkdir(exist_ok=True)
        (self.claude / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "accountUuid": "11111111-2222-3333-4444-555555555555",
            "emailAddress": "owner@example.com", "organizationUuid": "org_fixture"}}))

    def native_key(self):
        self.env["ANTHROPIC_API_KEY"] = "fixture-native-secret"
        self.env["USAGE_TEST_NATIVE_TOKEN"] = "fixture-native-secret"

    def openai_key(self):
        self.env["OPENAI_API_KEY"] = "fixture-openai-secret"
        self.env["OPENAI_API_KEY_ID"] = "key_fixture"
        self.env["USAGE_TEST_OPENAI_TOKEN"] = "fixture-openai-secret"

    def run_report(self, *args, success=True):
        self.fixture_path.write_text(json.dumps(self.fixture))
        result = subprocess.run(["bash", str(SCRIPT), *args], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        for secret in ("fixture-gateway-secret", "private-anthropic-secret",
                       "fixture-native-secret", "fixture-openai-secret"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()] \
            if self.calls_path.exists() else []

    def assert_total(self, output, source, amount):
        self.assertRegex(output, rf"(?m)^.*\b{source}\b.*\${re.escape(amount)}\s*$")

    def test_direct_claude_and_codex_work_without_gateway_credentials(self):
        self.claude_history()
        self.codex_history()
        output = self.run_report("30d")
        self.assertRegex(output.stdout, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("$", output.stdout)
        self.assertNotIn("BY UTC DAY", output.stdout)
        self.assertNotRegex(output.stdout, r"(?i)Claude|Codex|gateway key not configured")
        detail = self.run_report("detail", "30d")
        self.assert_total(detail.stdout, "CLAUDE", "4.80")
        self.assert_total(detail.stdout, "CODEX", "1.02")
        self.assertFalse(any(call["path"] == "/v1/report" for call in self.calls()))

    def test_no_configured_source_does_not_claim_zero_or_name_native_clients(self):
        output = self.run_report("today").stdout
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("$", output)
        self.assertNotRegex(output, r"(?i)Claude|Codex|BY UTC DAY")
        self.assertNotIn("No billing source configured", output)

    def test_native_client_setup_without_history_gets_exclusion_footer(self):
        self.claude.mkdir()
        (self.claude / ".credentials.json").write_text("{}")
        self.codex.mkdir()
        (self.codex / "auth.json").write_text("{}")
        output = self.run_report("today").stdout
        self.assertNotRegex(output, r"(?i)Claude|Codex|excluded")
        self.assertNotIn("$", output)

    def test_overridden_client_directories_count_as_detected_setup(self):
        self.claude.mkdir()
        self.codex.mkdir()
        output = self.run_report("today").stdout
        self.assertNotRegex(output, r"(?i)Claude|Codex|excluded")

    def test_gateway_only_claude_settings_do_not_imply_native_billing(self):
        self.claude.mkdir()
        (self.claude / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code",
            "ANTHROPIC_AUTH_TOKEN": "fixture-gateway-secret"}}))
        self.env["USAGE_TEST_TOKEN"] = "fixture-gateway-secret"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertNotIn("estimates excluded", output)

    def test_default_does_not_fetch_catalog_or_scan_native_histories(self):
        self.gateway_key()
        self.claude_history()
        self.codex_history()
        self.fixture["catalog_error"] = True
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["path"] == "/v1/models" for call in self.calls()))
        self.assertEqual([call["query"].get("group_by") for call in self.calls()
                          if call["path"] == "/v1/report"], [["day"]])
        self.assertFalse(list(self.state.rglob("*.sqlite3")))

    def test_gateway_billing_excludes_native_estimates_even_when_histories_exist(self):
        self.gateway_key()
        self.claude_history()
        self.codex_history()
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertNotRegex(output, r"(?m)^.*(?:CLAUDE|CODEX).*\$")
        self.assertNotIn("$4.80", output)
        self.assertNotIn("$1.02", output)
        self.assertNotRegex(output, r"(?i)Claude|Codex|excluded")

    def test_claude_personal_report_adds_post_discount_cost_to_total_and_utc_day(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "3.76")
        self.assert_total(output, "CLAUDE", "1.26")
        self.assertIn("REPORTED COST", output)
        self.assertIn("as of", output)
        day_row = next(line for line in output.splitlines() if line.startswith("  " + DAY))
        self.assertIn("$1.26", day_row)
        native = [call for call in self.calls() if call["host"] == "api.anthropic.com"]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]["query"].get("user_ids[]"),
                         ["11111111-2222-3333-4444-555555555555"])
        self.assertEqual(native[0]["query"].get("products[]"), ["claude_code"])
        self.assertEqual(native[0]["query"].get("bucket_width"), ["1d"])
        self.assertTrue(native[0]["expected_native_key"])
        self.assertEqual(native[0]["anthropic_version"], "2023-06-01")
        self.assertFalse(native[0]["has_auth"])

    def test_native_401_omits_claude_without_erasing_gateway_bill(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_error"] = True
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertNotRegex(output, r"(?m)^.*CLAUDE.*\$[\d,.]+$")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_native_key_without_own_identity_never_requests_organization_report(self):
        self.gateway_key()
        self.native_key()
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["host"] == "api.anthropic.com" for call in self.calls()))

    def test_native_report_for_other_actor_is_not_counted(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_response"]["data"][0]["actor"]["email_address"] = "other@example.com"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_matching_claude_user_id_with_conflicting_email_is_rejected(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        actor = self.fixture["native_response"]["data"][0]["actor"]
        actor["user_id"] = "11111111-2222-3333-4444-555555555555"
        actor["email_address"] = "other@example.com"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_claude_watermark_marks_newer_days_pending_without_zero_billing(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_response"]["data_refreshed_at"] = DAY + "T12:00:00Z"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "3.76")
        today_row = next(line for line in output.splitlines()
                         if line.startswith("  " + datetime.now(timezone.utc).date().isoformat()))
        self.assertIn("pending", today_row)
        self.assertRegex(today_row, r"pending\s+—$")

    def test_claude_row_after_watermark_cannot_enter_total(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_response"]["data_refreshed_at"] = DAY + "T12:00:00Z"
        self.fixture["native_response"]["data"][0]["starting_at"] = (
            datetime.now(timezone.utc).date().isoformat() + "T00:00:00Z")
        self.fixture["native_response"]["data"][0]["ending_at"] = (
            (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat() + "T00:00:00Z")
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_claude_report_pages_are_aggregated_before_display(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        first = json.loads(json.dumps(self.fixture["native_response"]))
        first["has_more"] = True
        first["next_page"] = "page-two"
        second = json.loads(json.dumps(self.fixture["native_response"]))
        second["data"][0]["amount"] = "100.000000"
        second["data"][0]["starting_at"] = (
            datetime.now(timezone.utc).date().isoformat() + "T00:00:00Z")
        second["data"][0]["ending_at"] = (
            (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat() + "T00:00:00Z")
        self.fixture["native_pages"] = {"first": first, "page-two": second}
        output = self.run_report("30d").stdout
        self.assert_total(output, "CLAUDE", "2.26")
        self.assert_total(output, "TOTAL", "4.76")
        native = [call for call in self.calls() if call["host"] == "api.anthropic.com"]
        self.assertEqual([call["query"].get("page") for call in native], [None, ["page-two"]])

    def test_duplicate_native_day_across_pages_is_not_double_billed(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        first = json.loads(json.dumps(self.fixture["native_response"]))
        first["has_more"] = True
        first["next_page"] = "page-two"
        self.fixture["native_pages"] = {"first": first, "page-two": self.fixture["native_response"]}
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_claude_report_splits_windows_at_31_days(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        empty = dict(self.fixture["native_response"], data=[])
        self.fixture["native_response"] = empty
        first_day = (datetime.now(timezone.utc).date() - timedelta(days=32)).isoformat()
        first = json.loads(json.dumps(empty))
        first["data"] = [{
            "actor": {"type": "user_actor", "user_id": "user_fixture",
                      "email_address": "owner@example.com"},
            "amount": "0", "currency": "USD", "starting_at": first_day + "T00:00:00Z",
            "ending_at": (datetime.fromisoformat(first_day) + timedelta(days=1)).date().isoformat()
                         + "T00:00:00Z"}]
        self.fixture["native_windows"] = {first_day + "T00:00:00Z": first}
        output = self.run_report("32d").stdout
        self.assert_total(output, "CLAUDE", "0.00")
        native = [call for call in self.calls() if call["host"] == "api.anthropic.com"]
        self.assertEqual(len(native), 2)
        for call in native:
            start = datetime.fromisoformat(call["query"]["starting_at"][0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(call["query"]["ending_at"][0].replace("Z", "+00:00"))
            self.assertLessEqual((end - start).days, 31)

    def test_bare_uuid_empty_report_does_not_claim_verified_zero(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_response"]["data"] = []
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_default_identity_path_reads_home_claude_json(self):
        self.gateway_key()
        self.native_key()
        self.env.pop("CLAUDE_CONFIG_DIR")
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "accountUuid": "11111111-2222-3333-4444-555555555555",
            "emailAddress": "owner@example.com", "organizationUuid": "org_fixture"}}))
        output = self.run_report("30d").stdout
        self.assert_total(output, "CLAUDE", "1.26")

    def test_gateway_settings_do_not_turn_ambiguous_env_key_into_native_key(self):
        self.gateway_key()
        self.native_identity()
        self.claude.mkdir(exist_ok=True)
        (self.claude / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh",
            "ANTHROPIC_AUTH_TOKEN": "fixture-gateway-secret"}}))
        self.env["ANTHROPIC_API_KEY"] = "fixture-gateway-secret"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["host"] == "api.anthropic.com" for call in self.calls()))

    def test_three_provider_report_sums_only_reported_daily_costs(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.openai_key()
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "4.50")
        self.assert_total(output, "CLAUDE", "1.26")
        self.assert_total(output, "OPENAI", "0.75")
        day_row = next(line for line in output.splitlines() if line.startswith("  " + DAY))
        self.assertIn("$4.50", day_row)
        openai = [call for call in self.calls() if call["host"] == "api.openai.com"]
        self.assertEqual(len(openai), 1)
        self.assertEqual(openai[0]["query"].get("api_key_ids[]"), ["key_fixture"])
        self.assertEqual(openai[0]["query"].get("group_by[]"), ["api_key_id"])
        self.assertTrue(openai[0]["expected_openai_key"])

    def test_openai_failure_keeps_other_reported_bills(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.openai_key()
        self.fixture["openai_error"] = True
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "3.76")
        self.assertIn("TOTAL · AVAILABLE BILLED", output)

    def test_openai_only_report_does_not_claim_no_source_configured(self):
        self.openai_key()
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "0.75")
        self.assertNotIn("No billing source configured", output)
        self.assertNotIn("gateway key not configured", output)

    def test_openai_wrong_result_key_rejects_report(self):
        self.gateway_key()
        self.openai_key()
        self.fixture["openai_response"]["data"][0]["results"][0]["api_key_id"] = "key_other"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_openai_missing_personal_key_id_never_queries_org_cost(self):
        self.gateway_key()
        self.openai_key()
        self.env.pop("OPENAI_API_KEY_ID")
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["host"] == "api.openai.com" for call in self.calls()))
        self.assertNotIn("INCOMPLETE", output)

    def test_openai_api_key_from_codex_auth_json(self):
        self.gateway_key()
        self.codex.mkdir()
        (self.codex / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "fixture-openai-secret"}))
        self.env["OPENAI_API_KEY_ID"] = "key_fixture"
        self.env["USAGE_TEST_OPENAI_TOKEN"] = "fixture-openai-secret"
        output = self.run_report("30d").stdout
        self.assert_total(output, "OPENAI", "0.75")

    def test_openai_admin_key_takes_priority_over_client_key(self):
        self.gateway_key()
        self.openai_key()
        self.env["OPENAI_ADMIN_KEY"] = "fixture-openai-admin-secret"
        self.env["USAGE_TEST_OPENAI_TOKEN"] = "fixture-openai-admin-secret"
        output = self.run_report("30d").stdout
        self.assert_total(output, "OPENAI", "0.75")
        self.assertNotIn("fixture-openai-admin-secret", output)

    def test_openai_explicit_admin_billing_key_works_with_client_proxy_base(self):
        self.gateway_key()
        self.openai_key()
        self.env["OPENAI_BASE_URL"] = "https://client-proxy.example.invalid"
        self.env["OPENAI_ADMIN_KEY"] = "fixture-openai-admin-secret"
        self.env["USAGE_TEST_OPENAI_TOKEN"] = "fixture-openai-admin-secret"
        output = self.run_report("30d").stdout
        self.assert_total(output, "OPENAI", "0.75")
        openai = [call for call in self.calls() if call["host"] == "api.openai.com"]
        self.assertEqual(len(openai), 1)
        self.assertEqual(openai[0]["query"].get("api_key_ids[]"), ["key_fixture"])
        self.assertTrue(openai[0]["expected_openai_key"])

    def test_openai_pages_sum_distinct_utc_days(self):
        self.gateway_key()
        self.openai_key()
        first = json.loads(json.dumps(self.fixture["openai_response"]))
        first["has_more"] = True
        first["next_page"] = "second"
        second = json.loads(json.dumps(self.fixture["openai_response"]))
        second["data"][0]["start_time"] += 86400
        second["data"][0]["end_time"] += 86400
        second["data"][0]["results"][0]["amount"]["value"] = 0.25
        self.fixture["openai_pages"] = {"first": first, "second": second}
        output = self.run_report("30d").stdout
        self.assert_total(output, "OPENAI", "1.00")
        openai = [call for call in self.calls() if call["host"] == "api.openai.com"]
        self.assertEqual([call["query"].get("page") for call in openai], [None, ["second"]])

    def test_openai_duplicate_day_in_later_page_discards_whole_source(self):
        self.gateway_key()
        self.openai_key()
        first = json.loads(json.dumps(self.fixture["openai_response"]))
        first["has_more"] = True
        first["next_page"] = "second"
        self.fixture["openai_pages"] = {"first": first, "second": self.fixture["openai_response"]}
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertIn("TOTAL · VERCEL BILLED", output)

    def test_openai_detail_keeps_estimates_separate_from_reported_cost(self):
        self.gateway_key()
        self.openai_key()
        self.codex_history()
        output = self.run_report("detail", "30d").stdout
        self.assertRegex(output, r"(?m)^REPORTED TOTAL: \$3\.25 \(Vercel \+ OpenAI\)\s*$")
        self.assertRegex(output, r"(?m)^CODEX · local estimate: ≈\$1\.02\s*$")
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+\$2\.50\s+\$0\.75\s+\$3\.25\s*$")
        self.assertRegex(daily, r"(?m)^\s+TOTAL\s+\$2\.50\s+\$0\.75\s+\$3\.25\s*$")
        self.assertNotIn("≈$1.02", daily)

    def test_codex_oauth_login_does_not_send_bearer_to_cost_api(self):
        self.gateway_key()
        self.codex.mkdir()
        (self.codex / "auth.json").write_text(json.dumps({"tokens": {"access_token": "oauth-secret"}}))
        self.env["OPENAI_API_KEY_ID"] = "key_fixture"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["host"] == "api.openai.com" for call in self.calls()))

    def test_custom_openai_base_and_gateway_key_are_never_sent_to_openai(self):
        self.gateway_key()
        self.openai_key()
        self.env["OPENAI_BASE_URL"] = "https://proxy.example.invalid"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["host"] == "api.openai.com" for call in self.calls()))

    def test_openai_and_gateway_redirects_cannot_forward_credentials(self):
        self.gateway_key()
        self.openai_key()
        self.fixture["openai_redirect"] = True
        self.fixture["gateway_redirect"] = True
        output = self.run_report("30d").stdout
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")
        self.assertFalse(any(call["host"] == "elsewhere.invalid" for call in self.calls()))

    def test_explicit_claude_analytics_key_precedes_managed_key(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.env["ANTHROPIC_ANALYTICS_API_KEY"] = "fixture-native-secret"
        self.env.pop("ANTHROPIC_API_KEY")
        output = self.run_report("30d").stdout
        self.assert_total(output, "CLAUDE", "1.26")

    def test_analytics_key_without_saved_identity_reports_omission(self):
        self.gateway_key()
        self.env["ANTHROPIC_ANALYTICS_API_KEY"] = "fixture-native-secret"
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertNotIn("INCOMPLETE", output)
        self.assertFalse(any(call["host"] == "api.anthropic.com" for call in self.calls()))

    def test_report_display_suppresses_subcent_values_without_rounding_total(self):
        self.gateway_key()
        for cents, expected in (("0.001", "$0.00"), ("0.009", "$0.00"),
                                ("0.010", "$0.01")):
            with self.subTest(cents=cents):
                self.fixture["daily_rows"][0]["total_cost"] = float(cents)
                output = self.run_report("30d").stdout
                self.assert_total(output, "TOTAL", expected[1:])
                self.assertNotIn("<$", output)

    def test_native_redirect_is_blocked_without_leaking_key(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_redirect"] = True
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertNotIn("fixture-native-secret", output)

    def test_oauth_only_credentials_are_not_sent_as_api_key(self):
        self.gateway_key()
        self.native_identity()
        (self.claude / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fixture-oauth-secret"}}))
        output = self.run_report("30d").stdout
        self.assert_total(output, "TOTAL", "2.50")
        self.assertFalse(any(call["host"] == "api.anthropic.com" for call in self.calls()))

    @unittest.skipUnless(sys.platform == "darwin", "macOS Keychain lookup")
    def test_managed_key_uses_overridden_config_service_and_account(self):
        self.gateway_key()
        self.native_identity()
        self.env["USAGE_TEST_NATIVE_TOKEN"] = "fixture-native-secret"
        keychain_calls = self.home / "security-calls"
        bindir = self.home / "bin"
        bindir.mkdir(exist_ok=True)
        security = bindir / "security"
        security.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$USAGE_TEST_SECURITY_CALLS\"\n"
                            "printf '%s' 'fixture-native-secret'\n")
        security.chmod(0o700)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env.get("PATH", "")
        self.env["USAGE_TEST_SECURITY_CALLS"] = str(keychain_calls)
        output = self.run_report("30d").stdout
        self.assert_total(output, "CLAUDE", "1.26")
        suffix = hashlib.sha256(str(self.claude).encode()).hexdigest()[:8]
        self.assertIn("Claude Code-" + suffix, keychain_calls.read_text())
        self.assertIn("-a " + self.env.get("USER", ""), keychain_calls.read_text())

    def test_daily_report_sums_all_utc_days_and_fills_missing_days(self):
        self.gateway_key()
        self.fixture["daily_rows"] = [
            {"day": DAY, "total_cost": 1.20, "request_count": 2},
            {"day": datetime.now(timezone.utc).date().isoformat(),
             "total_cost": 2.50, "request_count": 3}]
        output = self.run_report("2d").stdout
        self.assert_total(output, "TOTAL", "3.70")
        self.assert_total(output, "VERCEL", "3.70")
        day_rows = [line for line in output.splitlines() if re.match(r"  20\d\d-\d\d-\d\d", line)]
        self.assertEqual(len(day_rows), 3, output)
        self.assertTrue(any("$0.00" in line for line in day_rows), output)
        self.assertTrue(any("$1.20" in line for line in day_rows), output)
        self.assertTrue(any("$2.50" in line for line in day_rows), output)

    def test_model_query_failure_does_not_erase_valid_daily_billing(self):
        self.gateway_key()
        self.fixture["model_error"] = True
        for request_count in (3, "unknown"):
            with self.subTest(request_count=request_count):
                self.fixture["daily_rows"][0]["request_count"] = request_count
                output = self.run_report("detail", "30d").stdout
                self.assertRegex(output, r"(?m)^BILLED TOTAL: \$2\.50 \(Vercel only\)\s*$")
                self.assertRegex(output, r"(?m)^VERCEL.*billed: \$2\.50\b")
                self.assertNotIn("deepseek-v4-pro", output)
                self.assertIn("Vercel model breakdown unavailable; daily billing retained.",
                              " ".join(output.split()))

    def test_empty_successful_daily_report_is_verified_zero(self):
        self.gateway_key()
        self.fixture["daily_rows"] = []
        self.fixture["model_rows"] = []
        output = self.run_report("today").stdout
        self.assert_total(output, "TOTAL", "0.00")
        self.assertIn("BY UTC DAY", output)
        self.assertIn(datetime.now(timezone.utc).date().isoformat(), output)

    def test_daily_failure_after_success_never_reuses_previous_bill(self):
        self.gateway_key()
        self.assert_total(self.run_report("30d").stdout, "TOTAL", "2.50")
        self.fixture["report_error"] = True
        output = self.run_report("30d").stdout
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("$2.50", output)
        self.assertNotIn("BY UTC DAY", output)

    def test_invalid_daily_amount_does_not_display_a_bill(self):
        self.gateway_key()
        self.fixture["daily_rows"][0]["total_cost"] = -2.50
        output = self.run_report("30d").stdout
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("BY UTC DAY", output)

    def test_boolean_daily_charge_is_not_treated_as_one_dollar(self):
        self.gateway_key()
        self.fixture["daily_rows"][0]["total_cost"] = True
        output = self.run_report("30d").stdout
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("BY UTC DAY", output)

    def test_bad_daily_request_count_does_not_discard_valid_cost(self):
        self.gateway_key()
        self.fixture["daily_rows"][0]["request_count"] = "unknown"
        output = self.run_report("detail", "30d").stdout
        self.assertRegex(output, r"(?m)^BILLED TOTAL: \$2\.50 \(Vercel only\)\s*$")
        self.assertNotIn("across 0 requests", output)

    def test_bad_optional_model_id_cannot_crash_billed_detail(self):
        self.gateway_key()
        self.fixture["model_rows"][0]["model"] = 123
        output = self.run_report("detail", "30d").stdout
        self.assertRegex(output, r"(?m)^BILLED TOTAL: \$2\.50 \(Vercel only\)\s*$")
        self.assertNotIn("Traceback", output)

    def test_daily_amount_overflow_does_not_display_infinite_bill(self):
        self.gateway_key()
        self.fixture["daily_rows"] = [
            {"day": DAY, "total_cost": 1e308, "request_count": 1},
            {"day": datetime.now(timezone.utc).date().isoformat(),
             "total_cost": 1e308, "request_count": 1}]
        output = self.run_report("30d").stdout
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("$inf", output)
        self.assertNotIn("BY UTC DAY", output)

    def test_gateway_totals_use_current_key_without_person_name(self):
        self.gateway_key()
        self.write_jsonl(self.claude / "projects/example/session.jsonl", [
            self.assistant("gen_example", "wrong-transcript-model", input_tokens=1000000)])
        output = self.run_report("30d")
        self.assert_total(output.stdout, "VERCEL", "2.50")
        self.assertNotIn("deepseek-v4-pro", output.stdout)
        self.assertRegex(output.stdout, r"(?i)(?:this|current|authenticated).*key")
        reports = [call for call in self.calls() if call["path"] == "/v1/report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["query"].get("group_by"), ["day"])
        self.assertTrue(all(call["query"].get("api_key_id") == ["self"] for call in reports))
        self.assertTrue(all(call["expected_auth"] for call in reports))
        detail = self.run_report("detail", "30d")
        self.assertRegex(detail.stdout, r"deepseek-v4-pro.*\$0\.50")
        self.assertRegex(detail.stdout, r"claude-sonnet-4\.6.*\$2\.00")

    def test_gateway_billing_needs_no_local_transcript_or_generation_lookups(self):
        self.gateway_key()
        output = self.run_report("30d")
        self.assert_total(output.stdout, "VERCEL", "2.50")
        self.assertNotIn("deepseek-v4-pro", output.stdout)
        self.assertFalse(any(call["path"] == "/v1/generation" for call in self.calls()))
        daily = [line for line in output.stdout.splitlines() if DAY in line or
                 datetime.fromisoformat(DAY).strftime("%b") + " " + str(int(DAY[-2:])) in line]
        self.assertTrue(any("$2.50" in line for line in daily), output.stdout)
        self.write_jsonl(self.claude / "projects/example/session.jsonl", [
            self.assistant("gen_example", "deepseek/deepseek-v4-pro", input_tokens=1000000)])
        self.assert_total(self.run_report("30d").stdout, "VERCEL", "2.50")
        self.assertFalse(any(call["path"] == "/v1/generation" for call in self.calls()))

    def test_native_anthropic_credential_is_never_sent_to_vercel(self):
        self.claude_history()
        self.env.update({"ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                         "ANTHROPIC_API_KEY": "private-anthropic-secret"})
        output = self.run_report("30d")
        self.assertNotIn("$4.80", output.stdout)
        self.assert_total(self.run_report("detail", "30d").stdout, "CLAUDE", "4.80")
        self.assertFalse(any(call["path"] == "/v1/report" for call in self.calls()))
        self.assertTrue(all(not call["has_auth"] for call in self.calls()))

    def test_gateway_credentials_from_environment(self):
        self.env.update({"ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code",
                         "ANTHROPIC_AUTH_TOKEN": "fixture-gateway-secret",
                         "USAGE_TEST_TOKEN": "fixture-gateway-secret"})
        self.assert_total(self.run_report("30d").stdout, "VERCEL", "2.50")

    def test_gateway_credentials_from_claude_settings(self):
        self.claude.mkdir()
        settings = self.claude / "settings.json"
        settings.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code",
            "ANTHROPIC_AUTH_TOKEN": "fixture-gateway-secret"}}))
        before = settings.read_bytes()
        self.env["USAGE_TEST_TOKEN"] = "fixture-gateway-secret"
        self.assert_total(self.run_report("30d").stdout, "VERCEL", "2.50")
        self.assertEqual(settings.read_bytes(), before)

    def test_configured_gateway_helper_is_discovered_without_changing_settings(self):
        self.claude.mkdir()
        helper = self.home / "key helper.sh"
        helper.write_text("#!/bin/sh\nprintf '%s' 'fixture-gateway-secret'\n")
        settings = self.claude / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL":
            "https://ai-gateway.vercel.sh/claude-code"}, "apiKeyHelper": "sh " + shlex.quote(str(helper))}))
        before = settings.read_bytes()
        self.env["USAGE_TEST_TOKEN"] = "fixture-gateway-secret"
        self.assert_total(self.run_report("30d").stdout, "VERCEL", "2.50")
        self.assertEqual(settings.read_bytes(), before)

    def test_native_helper_is_not_run_for_gateway_discovery(self):
        self.claude.mkdir()
        marker = self.home / "helper-was-run"
        helper = self.home / "native-helper.sh"
        helper.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
        (self.claude / "settings.json").write_text(json.dumps({"apiKeyHelper": "sh " + shlex.quote(str(helper))}))
        self.claude_history()
        self.run_report("30d")
        self.assertFalse(marker.exists())

    def test_nested_workers_model_switches_and_streaming_duplicates(self):
        path = self.claude_history()
        main_row = self.assistant(input_tokens=1000000, output_tokens=100000, cache_read_input_tokens=1000000)
        self.write_jsonl(path.parent / "session/subagents/nested/worker.jsonl", [main_row,
            self.assistant("msg_opus", "claude-opus-4-6", input_tokens=1000000)])
        output = self.run_report("detail", "30d")
        self.assert_total(output.stdout, "CLAUDE", "9.80")
        self.assertRegex(output.stdout, r"claude-opus-4\.6.*\$5\.00")
        self.assertRegex(output.stdout, r"claude-sonnet-4\.6.*\$4\.80")

    def test_archived_codex_counts_but_gateway_provider_does_not(self):
        self.codex_history(folder="archived_sessions")
        self.codex_history(provider="vercel")
        self.assert_total(self.run_report("detail", "30d").stdout, "CODEX", "1.02")

    def test_missing_codex_model_is_reported_as_incomplete_history_not_missing_price(self):
        self.codex_history()
        path = self.codex / "sessions/rollout-example.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(1, {"type": "event_msg", "timestamp": STAMP,
                        "payload": {"type": "token_count", "info": {
                            "total_token_usage": {"input_tokens": 75090, "output_tokens": 0,
                                                  "total_tokens": 75090},
                            "last_token_usage": {"input_tokens": 0, "cached_input_tokens": 0,
                                                 "cache_write_input_tokens": 0, "output_tokens": 0,
                                                 "reasoning_output_tokens": 0,
                                                 "total_tokens": 75090}}}})
        self.write_jsonl(path, rows)
        output = self.run_report("detail", "30d").stdout
        self.assertIn("CODEX · local estimate: ≈$1.02 + unpriced", " ".join(output.split()))
        self.assertIn("Unpriced local usage: Codex · 1 record · 75,090 reported tokens · missing model", " ".join(output.split()))
        self.assertNotIn("Unknown catalog price:", output)
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+≈\$1\.02\*\s*$")

    def test_named_codex_model_without_catalog_price_remains_unpriced(self):
        self.codex_history()
        self.fixture["catalog"] = {"data": CATALOG["data"][:2]}
        output = self.run_report("detail", "30d").stdout
        self.assertIn("Unpriced local usage: Codex · 1 record · price unavailable", " ".join(output.split()))
        self.assertNotIn("Missing model:", output)
        self.assertRegex(output, r"CODEX gpt-5\s+1\s+800\.0K\s+100\.0K\s+200\.0K\s+0\s+unpriced")

    def test_usage_cache_not_stale_after_history_changes_or_window_switch(self):
        path = self.claude_history()
        older = self.assistant("msg_older", "claude-opus-4-6", input_tokens=1000000)
        older["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with path.open("a") as stream:
            stream.write(json.dumps(older) + "\n")
        self.assert_total(self.run_report("detail", "1d").stdout, "CLAUDE", "4.80")
        with path.open("a") as stream:
            stream.write(json.dumps(self.assistant("msg_opus", "claude-opus-4-6", input_tokens=1000000)) + "\n")
        self.assert_total(self.run_report("detail", "30d").stdout, "CLAUDE", "14.80")
        path.unlink()
        output = self.run_report("detail", "30d").stdout
        self.assertNotIn("CLAUDE · local estimate", output)
        self.assertNotIn("$14.80", output)

    def test_gateway_failure_reports_http_status_without_claiming_zero_bill(self):
        self.claude_history()
        self.gateway_key()
        self.fixture["report_error"] = True
        output = self.run_report("30d")
        self.assertRegex(output.stdout, r"(?m)^TOTAL[^$]*—$")
        self.assertNotIn("$", output.stdout)
        self.assertIn("TOTAL · VERCEL UNAVAILABLE", output.stdout)

    def test_gateway_lookalike_host_does_not_receive_anthropic_credentials(self):
        self.env.update({"ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh.example.org/claude-code",
                         "ANTHROPIC_AUTH_TOKEN": "private-anthropic-secret"})
        self.claude_history()
        self.run_report("30d")
        self.assertTrue(all(not call["has_auth"] for call in self.calls()))

    def test_codex_long_context_tier_uses_full_prompt_including_cached_input(self):
        self.codex_history()
        self.fixture["catalog"] = json.loads(json.dumps(CATALOG))
        pricing = self.fixture["catalog"]["data"][2]["pricing"]
        pricing.update({
            "input_tiers": [{"min": 1000000, "cost": "0.000002"}],
            "output_tiers": [{"min": 1000000, "cost": "0.000004"}],
            "input_cache_read_tiers": [{"min": 1000000, "cost": "0.00000025"}]})
        self.assert_total(self.run_report("detail", "30d").stdout, "CODEX", "2.05")

    def test_malformed_local_price_tier_does_not_block_billed_detail(self):
        self.gateway_key()
        self.codex_history()
        self.fixture["catalog"] = json.loads(json.dumps(CATALOG))
        for tier in ("bad-tier", {"min": 0}):
            with self.subTest(tier=tier):
                self.fixture["catalog"]["data"][2]["pricing"]["input_tiers"] = [tier]
                output = self.run_report("detail", "30d").stdout
                self.assertRegex(output, r"(?m)^BILLED TOTAL: \$2\.50 \(Vercel only\)\s*$")
                self.assertIn("unpriced", output)

    def test_one_hour_cache_writes_are_priced_separately(self):
        self.write_jsonl(self.claude / "projects/example/session.jsonl", [
            self.assistant(cache_creation_input_tokens=1000000,
                           cache_creation={"ephemeral_1h_input_tokens": 1000000})])
        self.assert_total(self.run_report("detail", "30d").stdout, "CLAUDE", "6.00")

    def test_corrupt_metadata_cache_falls_back_to_histories(self):
        self.claude_history()
        self.run_report("detail", "30d")
        caches = list(self.state.rglob("*.sqlite3"))
        self.assertTrue(caches)
        caches[0].write_bytes(b"invalid sqlite data")
        self.assert_total(self.run_report("detail", "30d").stdout, "CLAUDE", "4.80")

    def test_detail_reports_gateway_tokens_without_local_transcripts(self):
        self.gateway_key()
        result = self.run_report("detail", "30d")
        self.assertIn("deepseek", result.stdout)
        self.assertRegex(result.stdout, r"(?:1\.0M|1,000,000|1000000)")
        self.assertIn("2.50", result.stdout)

    def test_detail_keeps_local_estimates_separate_from_billed_total(self):
        self.gateway_key()
        self.claude_history()
        self.codex_history()
        output = self.run_report("detail", "30d").stdout
        self.assertRegex(output, r"(?m)^BILLED TOTAL: \$2\.50 \(Vercel only\)\s*$")
        self.assert_total(output, "CLAUDE", "4.80")
        self.assert_total(output, "CODEX", "1.02")
        self.assertRegex(output, r"(?m)^LOCAL ESTIMATE: ≈\$5\.82\s*$")
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+\$2\.50\s+≈\$4\.80\s+≈\$1\.02\s+≈\$8\.32\s*$")
        self.assertRegex(daily, r"(?m)^\s+TOTAL\s+\$2\.50\s+≈\$4\.80\s+≈\$1\.02\s+≈\$8\.32\s*$")
        self.assertRegex(daily, r"billed\s+local\s+local")
        self.assertEqual(output.count("BY UTC DAY"), 1)
        self.assertNotIn("SUBTOTAL", daily)
        self.assertNotRegex(output, r"(?m)^BILLED TOTAL:.*8\.32")

    def test_detail_shows_local_total_and_daily_estimates_when_billing_is_unavailable(self):
        self.claude_history()
        self.codex_history()
        output = self.run_report("detail", "30d").stdout
        self.assertIn("BILLED TOTAL: unavailable", output)
        self.assertRegex(output, r"(?m)^LOCAL ESTIMATE: ≈\$5\.82\s*$")
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+≈\$4\.80\s+≈\$1\.02\s+≈\$5\.82\s*$")
        self.assertRegex(daily, r"(?m)^\s+TOTAL\s+≈\$4\.80\s+≈\$1\.02\s+≈\$5\.82\s*$")
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertRegex(daily, rf"(?m)^\s+{today}\s+—\s+—\s+—\s*$")

    def test_detail_marks_only_affected_local_day_and_keeps_other_days_priced(self):
        self.claude_history()
        self.codex_history()
        path = self.codex / "sessions/rollout-example.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        missing_day = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
        rows.insert(1, {"type": "event_msg", "timestamp": missing_day + "T12:00:00Z",
                        "payload": {"type": "token_count", "info": {
                            "total_token_usage": {"total_tokens": 75090},
                            "last_token_usage": {"input_tokens": 0, "output_tokens": 0,
                                                 "total_tokens": 75090}}}})
        self.write_jsonl(path, rows)
        output = self.run_report("detail", "30d").stdout
        self.assertRegex(output, r"(?m)^LOCAL ESTIMATE: ≈\$5\.82\s*$")
        self.assertIn("Unpriced local usage: Codex · 1 record · 75,090 reported tokens", " ".join(output.split()))
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+≈\$4\.80\s+≈\$1\.02\s+≈\$5\.82\s*$")
        self.assertRegex(daily, rf"(?m)^\s+{missing_day}\s+—\s+unpriced\s+unpriced\s*$")
        self.assertRegex(daily, r"(?m)^\s+TOTAL\s+≈\$4\.80\s+≈\$1\.02\*\s+≈\$5\.82\*\s*$")
        self.assertIn("75,090 reported tokens", " ".join(output.split()))

    def test_detail_unknown_model_with_no_priced_usage_never_claims_zero_cost(self):
        self.codex_history()
        path = self.codex / "sessions/rollout-example.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.write_jsonl(path, [rows[0], rows[2]])
        output = self.run_report("detail", "30d").stdout
        self.assertIn("LOCAL ESTIMATE: unavailable (no priced records)", " ".join(output.split()))
        self.assertNotRegex(output, r"(?m)^LOCAL ESTIMATE: ≈\$0\.00")
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+unpriced\s*$")

    def test_detail_uses_provider_colors_in_a_terminal_and_respects_no_color(self):
        self.claude_history()
        self.codex_history()
        self.boundary.joinpath("sitecustomize.py").write_text(
            HTTP_BOUNDARY + "\nimport sys\nsys.stdout.isatty = lambda: True\n")
        self.env.pop("NO_COLOR")
        colored = self.run_report("detail", "30d").stdout
        self.assertIn("\x1b[38;2;217;89;38mCLAUDE", colored)
        self.assertIn("\x1b[38;2;25;158;112mCODEX", colored)
        self.env["NO_COLOR"] = "1"
        self.assertNotIn("\x1b[", self.run_report("detail", "30d").stdout)

    def test_dashboard_claude_daily_columns_are_99_visible_columns(self):
        self.native_identity()
        self.native_key()
        self.gateway_key()
        for gateway in (True, False):
            with self.subTest(gateway=gateway):
                if not gateway:
                    self.env.pop("AI_GATEWAY_API_KEY")
                output = self.run_report("30d").stdout
                lines = [line for line in output.splitlines()
                         if "BY UTC DAY" in line or re.match(r"^  20\d\d-", line)]
                self.assertTrue(lines)
                self.assertEqual({len(line) for line in lines}, {99}, output)

    def test_detail_pending_claude_day_still_shows_available_gateway_cost(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_response"]["data_refreshed_at"] = DAY + "T12:00:00Z"
        today = datetime.now(timezone.utc).date().isoformat()
        self.fixture["daily_rows"].append({"day": today, "total_cost": 0.5})
        output = self.run_report("detail", "30d").stdout
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{today}\s+\$0\.50\s+pending\s+\$0\.50\*\s*$")
        self.assertIn("REPORTED SUBTOTAL:", output)

    def test_detail_with_one_daily_provider_has_no_duplicate_subtotal(self):
        self.gateway_key()
        output = self.run_report("detail", "30d").stdout
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+\$2\.50\s*$")
        self.assertNotIn("SUBTOTAL", daily)
        self.assertRegex(daily, r"(?m)^\s+TOTAL\s+\$2\.50\s*$")
        heading = next(line for line in daily.splitlines() if "DATE" in line)
        self.assertNotIn("TOTAL", heading)

    def test_detail_uses_original_99_column_layout_with_and_without_color(self):
        self.gateway_key()
        self.claude_history()
        self.codex_history()
        self.boundary.joinpath("sitecustomize.py").write_text(
            HTTP_BOUNDARY + "\nimport sys\nsys.stdout.isatty = lambda: True\n")
        for width, color in ((50, False), (50, True), (99, False), (99, True)):
            with self.subTest(width=width, color=color):
                self.env["COLUMNS"] = str(width)
                if color:
                    self.env.pop("NO_COLOR", None)
                else:
                    self.env["NO_COLOR"] = "1"
                output = self.run_report("detail", "30d").stdout
                plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
                widths = {len(line) for line in plain.splitlines() if line.strip()}
                self.assertEqual(widths, {99}, plain)
                daily = plain.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
                row = next(line for line in daily.splitlines() if DAY in line)
                heading = next(line for line in daily.splitlines() if "DATE" in line)
                self.assertEqual(heading.index("DATE"), row.index(DAY))
                ends = [match.end() for match in re.finditer(r"[\d.]+", row) if match.start() > 12]
                self.assertEqual(ends, [heading.index(label) + len(label)
                                        for label in ("VERCEL", "CLAUDE", "CODEX", "TOTAL")])
                self.assertEqual(ends[-1], 99)
                footer = next(line for line in daily.splitlines() if line.lstrip().startswith("TOTAL"))
                self.assertEqual(ends, [match.end() for match in re.finditer(r"[\d.]+", footer)])

    def test_detail_preserves_long_model_names_and_large_costs(self):
        self.gateway_key()
        self.env["COLUMNS"] = "50"
        model = "claude-sonnet-very-long-context-preview-2026"
        self.fixture["daily_rows"][0]["total_cost"] = 123456789.12
        self.fixture["model_rows"] = [{"model": model, "total_cost": 123456789.12}]
        output = self.run_report("detail", "30d").stdout
        self.assertIn(model, output)
        self.assertIn("$123,456,789.12", output)
        self.assertTrue(all(len(line) == 99 for line in output.splitlines() if line.strip()), output)

    def test_detail_total_row_sums_raw_daily_costs_before_rounding(self):
        self.gateway_key()
        today = datetime.now(timezone.utc).date().isoformat()
        older = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        self.fixture["daily_rows"] = [{"day": DAY, "total_cost": 0.004},
                                      {"day": today, "total_cost": 0.004},
                                      {"day": older, "total_cost": 0.004}]
        self.fixture["model_rows"] = [{"model": "subcent-model", "total_cost": 0.012}]
        output = self.run_report("detail", "30d").stdout
        daily = output.split("BY UTC DAY", 1)[1].split("BY MODEL", 1)[0]
        self.assertRegex(daily, rf"(?m)^\s+{DAY}\s+\$0\.00\s*$")
        self.assertRegex(daily, r"(?m)^\s+TOTAL\s+\$0\.01\s*$")

    def test_detail_wide_model_rows_keep_tokens_and_cost_on_one_line(self):
        self.codex_history()
        output = self.run_report("detail", "30d").stdout
        models = output.split("BY MODEL", 1)[1]
        self.assertRegex(models, r"(?m)^CODEX gpt-5\s+1\s+800\.0K\s+100\.0K\s+200\.0K\s+0\s+≈\$1\.02\s*$")

    def test_detail_omits_only_vercel_models_with_zero_cost_and_zero_tokens(self):
        self.gateway_key()
        self.fixture["daily_rows"][0]["total_cost"] = 2.504
        self.fixture["model_rows"] = [
            {"model": "paid-model", "total_cost": 2.5, "request_count": 1},
            {"model": "subcent-model", "total_cost": 0.004, "request_count": 1},
            {"model": "empty-model", "total_cost": 0, "request_count": 4},
            {"model": "free-with-tokens", "total_cost": 0, "request_count": 1,
             "input_tokens": 10}]
        output = self.run_report("detail", "30d").stdout
        models = output.split("BY MODEL", 1)[1]
        self.assertIn("paid-model", models)
        self.assertIn("subcent-model", models)
        self.assertIn("free-with-tokens", models)
        self.assertNotIn("empty-model", models)
        self.assertRegex(models, r"Vercel: 1 empty model rows? hidden\.")

    def test_mixed_native_environment_and_gateway_settings_do_not_leak_native_token(self):
        self.claude.mkdir()
        (self.claude / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code",
            "ANTHROPIC_AUTH_TOKEN": "fixture-gateway-secret"}}))
        self.env.update({"ANTHROPIC_AUTH_TOKEN": "private-anthropic-secret",
                         "USAGE_TEST_TOKEN": "fixture-gateway-secret"})
        self.run_report("30d")
        self.assertTrue(all(not call["has_auth"] or call["expected_auth"] for call in self.calls()))

    def test_gateway_environment_does_not_reuse_native_settings_token(self):
        self.claude.mkdir()
        (self.claude / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "private-anthropic-secret"}}))
        self.env["ANTHROPIC_BASE_URL"] = "https://ai-gateway.vercel.sh/claude-code"
        self.run_report("30d")
        self.assertTrue(all(not call["has_auth"] for call in self.calls()))

    def test_native_key_prefix_is_rejected_even_with_gateway_url(self):
        secret = "sk-ant-api03-private-anthropic-secret"
        self.env.update({"ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code",
                         "ANTHROPIC_API_KEY": secret})
        output = self.run_report("30d")
        self.assertNotIn(secret, output.stdout + output.stderr)
        self.assertTrue(all(not call["has_auth"] for call in self.calls()))

    def test_discovered_api_key_precedes_auth_token_like_claude(self):
        self.env.update({"ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code",
                         "ANTHROPIC_API_KEY": "fixture-gateway-secret",
                         "ANTHROPIC_AUTH_TOKEN": "different-gateway-secret",
                         "USAGE_TEST_TOKEN": "fixture-gateway-secret"})
        self.assert_total(self.run_report("30d").stdout, "VERCEL", "2.50")
        self.assertTrue(all(call["expected_auth"] for call in self.calls()
                            if call["path"] == "/v1/report"))

    def test_dashboard_money_columns_stay_aligned(self):
        self.claude_history()
        self.codex_history()
        self.gateway_key()
        output = self.run_report("30d").stdout
        monetary_rows = [line for line in output.splitlines() if re.search(r"\$[\d,.]+\s*$", line)]
        self.assertGreater(len(monetary_rows), 3)
        self.assertEqual({len(line) for line in monetary_rows}, {99}, output)
        self.assertNotIn("deepseek-v4-pro", output)

    def test_unavailable_gateway_is_not_displayed_as_zero_on_daily_rows(self):
        self.claude_history()
        output = self.run_report("30d").stdout
        self.assertNotIn("BY UTC DAY", output)
        self.assertRegex(output, r"(?m)^TOTAL[^$]*—$")

    def test_copied_codex_rollout_is_not_counted_again_when_archived(self):
        self.codex_history()
        source = self.codex / "sessions/rollout-example.jsonl"
        target = self.codex / "archived_sessions/rollout-example.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())
        self.assert_total(self.run_report("detail", "30d").stdout, "CODEX", "1.02")

    def test_gateway_report_disagreement_keeps_daily_billed_total(self):
        self.gateway_key()
        self.fixture["model_rows"] = []
        output = self.run_report("detail", "30d").stdout
        self.assertRegex(output, r"(?m)^VERCEL.*billed: \$2\.50\b")
        total_line = next(line for line in output.splitlines() if line.startswith("BILLED TOTAL"))
        self.assertTrue(total_line.rstrip().endswith("$2.50 (Vercel only)"), total_line)
        self.assertIn("Vercel model breakdown unavailable; daily billing retained.",
                      " ".join(output.split()))

    def test_detail_keeps_gateway_models_when_daily_billing_differs(self):
        self.gateway_key()
        self.fixture["model_rows"] = [
            {"model": "deepseek/deepseek-v4-pro", "total_cost": 0.11226016,
             "request_count": 6, "input_tokens": 61957, "output_tokens": 396,
             "cached_input_tokens": 147200, "cache_creation_input_tokens": 0},
            {"model": "deepseek/deepseek-v4.1-flash", "total_cost": 0.0125045,
             "request_count": 1, "input_tokens": 40115, "output_tokens": 9,
             "cached_input_tokens": 0, "cache_creation_input_tokens": 0}]
        for cost, requests, billed in ((0.11226016, 6, "0.11"),
                                       (0.13726916, 8, "0.14"), (0, 0, "0.00")):
            with self.subTest(daily_cost=cost):
                self.fixture["daily_rows"] = [
                    {"day": DAY, "total_cost": cost, "request_count": requests}]
                output = self.run_report("detail", "2d").stdout
                self.assertRegex(output, rf"(?m)^BILLED TOTAL: \${re.escape(billed)} \(Vercel only\)\s*$")
                self.assertIn("BY MODEL", output)
                models = output.split("BY MODEL", 1)[1]
                self.assertRegex(models, r"(?m)^VERCEL deepseek-v4-pro\s+6\s+.*\$0\.11\s*$")
                self.assertRegex(models, r"(?m)^VERCEL deepseek-v4\.1-flash\s+1\s+.*\$0\.01\s*$")
                self.assertNotIn("model breakdown unavailable", output)

    def test_detail_catalog_failure_keeps_gateway_billing_available(self):
        self.claude_history()
        self.gateway_key()
        self.fixture["catalog_error"] = True
        output = self.run_report("detail", "30d")
        self.assertRegex(output.stdout, r"(?m)^BILLED TOTAL: \$2\.50 \(Vercel only\)\s*$")
        self.assertIn("Unpriced local usage: Claude · 1 record · catalog unavailable", output.stdout)

    def test_detail_empty_report_and_invalid_arguments(self):
        self.gateway_key()
        self.fixture["daily_rows"] = []
        self.fixture["model_rows"] = []
        output = self.run_report("detail", "today")
        self.assertIn("0.00", output.stdout)
        self.assertNotIn("Traceback", output.stderr)
        output = self.run_report("not-a-period", success=False)
        self.assertNotEqual(output.returncode, 0)
        self.assertRegex(output.stderr + output.stdout, r"(?i)usage")

    def test_successful_vercel_total_ignores_failed_native_lookup(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_error"] = True
        dashboard = self.run_report("30d").stdout
        total = next(line for line in dashboard.splitlines() if line.startswith("TOTAL"))
        self.assertTrue(total.startswith("TOTAL · VERCEL BILLED"), total)
        self.assertTrue(total.rstrip().endswith("$2.50"), total)
        self.assertNotIn("INCOMPLETE", dashboard)
        detail = self.run_report("detail", "30d").stdout
        self.assertIn("BILLED TOTAL: $2.50 (Vercel only)", detail)
        self.assertNotIn("Billing unavailable: Claude", detail)
        self.assertNotIn("estimated from local tokens", detail)
        self.assertNotIn("configured provider billing failed", detail)
        self.assertIn("BY UTC DAY · BILLING", detail)
        self.assertNotIn("BY UTC DAY · BILLING AND ESTIMATES", detail)
        for stale in ("Local estimates use saved history/list prices.",
                      "Reported totals exclude local estimates.",
                      "Vercel billing covers this key. Days are UTC.",
                      "— No saved usage on this day."):
            self.assertNotIn(stale, detail)

    def test_multi_provider_total_with_failed_third_source_is_available_billed(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.openai_key()
        self.fixture["openai_error"] = True
        dashboard = self.run_report("30d").stdout
        self.assertRegex(dashboard, r"(?m)^TOTAL · AVAILABLE BILLED.*\$3\.76$")
        detail = self.run_report("detail", "30d").stdout
        self.assertIn("AVAILABLE BILLED: $3.76 (Vercel + Claude)", detail)
        self.assertNotIn("Billing unavailable: OpenAI", detail)
        self.assertNotIn("configured provider billing failed", detail)

    def test_primary_vercel_failure_remains_visible_without_false_zero(self):
        self.gateway_key()
        self.fixture["report_error"] = True
        dashboard = self.run_report("30d").stdout
        self.assertRegex(dashboard, r"(?m)^TOTAL · VERCEL UNAVAILABLE.*—$")
        self.assertNotIn("$0.00", dashboard)
        detail = self.run_report("detail", "30d").stdout
        self.assertIn("BILLED TOTAL: unavailable", detail)
        self.assertEqual(detail.count("Billing unavailable: Vercel"), 1)
        self.assertNotIn("$0.00", detail)

    def test_local_token_fallback_note_tracks_only_unbilled_priced_sources(self):
        self.claude_history()
        claude_only = self.run_report("detail", "30d").stdout
        self.assertIn("* Claude estimated from local tokens.", claude_only)
        self.assertNotIn("Billing unavailable: Claude", claude_only)
        self.assertNotIn("≈ Local estimate from saved history; billed totals exclude it.",
                         claude_only)
        self.codex_history()
        both = self.run_report("detail", "30d").stdout
        self.assertIn("* Claude and Codex estimated from local tokens.", both)
        self.native_identity()
        self.native_key()
        claude_billed = self.run_report("detail", "30d").stdout
        self.assertIn("* Codex estimated from local tokens.", claude_billed)
        self.assertNotIn("* Claude estimated from local tokens.", claude_billed)
        self.openai_key()
        both_billed = self.run_report("detail", "30d").stdout
        self.assertNotIn("estimated from local tokens", both_billed)
        self.assertIn("REPORTED TOTAL:", both_billed)

    def test_unpriced_local_note_is_single_and_keeps_reported_tokens(self):
        self.codex_history()
        path = self.codex / "sessions/rollout-example.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(1, {"type": "event_msg", "timestamp": STAMP,
                        "payload": {"type": "token_count", "info": {
                            "total_token_usage": {"input_tokens": 75090, "output_tokens": 0,
                                                  "total_tokens": 75090},
                            "last_token_usage": {"input_tokens": 0, "cached_input_tokens": 0,
                                                 "output_tokens": 0, "total_tokens": 75090}}}})
        self.write_jsonl(path, rows)
        detail = self.run_report("detail", "30d").stdout
        self.assertIn("Unpriced local usage: Codex", detail)
        self.assertIn("75,090 reported tokens", detail)
        self.assertEqual(detail.count("Unpriced local usage: Codex"), 1)
        self.assertNotIn("Excludes 1 unpriced record", detail)
        self.assertNotIn("Missing model:", detail)
        self.assertNotIn("Unknown catalog price:", detail)
        self.assertNotIn("Local estimates use saved history/list prices.", detail)
        self.assertNotRegex(detail, r"(?m)^LOCAL ESTIMATE: ≈\$0\.00")

    def test_new_tty_emphasis_and_no_color_keep_99_visible_columns(self):
        self.gateway_key()
        self.native_identity()
        self.native_key()
        self.fixture["native_error"] = True
        self.boundary.joinpath("sitecustomize.py").write_text(
            HTTP_BOUNDARY + "\nimport sys\nsys.stdout.isatty = lambda: True\n")
        self.env.pop("NO_COLOR")
        colored = self.run_report("30d").stdout
        self.assertIn("\x1b[1mAI usage", colored)
        self.assertIn("\x1b[2mFrom:", colored)
        self.assertIn("\x1b[1mTOTAL · VERCEL BILLED", colored)
        detail = self.run_report("detail", "30d").stdout
        self.assertNotIn("Billing unavailable: Claude", detail)
        self.fixture["report_error"] = True
        warned = self.run_report("detail", "30d").stdout
        self.assertIn("\x1b[1;38;2;", warned)
        self.assertIn("Billing unavailable: Vercel", warned)
        for output in (colored, detail, warned):
            plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
            self.assertTrue(all(len(line) == 99 for line in plain.splitlines() if line.strip()))
        self.env["NO_COLOR"] = "1"
        self.assertNotIn("\x1b[", self.run_report("detail", "30d").stdout)

    def test_users_ranking_title_matches_selected_grouping(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "Avery Sample", "total_cost": "1", "request_count": 1}]
        self.assertIn("AI Gateway user ranking · by spend", self.run_report("users").stdout)
        self.fixture["team_rows"] = [
            {"provider": "anthropic", "total_cost": "1", "request_count": 1}]
        self.assertIn("AI Gateway usage · by provider",
                      self.run_report("users", "--by", "provider").stdout)

    def test_users_rank_all_people_and_keep_personal_gateway_scoped(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "someone@Alex-Morgan-ABCDEF12", "total_cost": "3.25",
             "request_count": 3},
            {"api_key_name": "[x's Alex Morgan ABCDEF12] laptop", "total_cost": "2.75",
             "request_count": 2},
            {"api_key_name": "unknown-service-prod", "total_cost": "4.00",
             "request_count": 4}]
        output = self.run_report("users", "today").stdout
        self.assertIn("$10.00", output)
        self.assertRegex(output, r"(?m)^\s*1\s+Alex Morgan\b.*\b5\b.*\$6\.00\s*$")
        self.assertRegex(output, r"(?m)^\s*2\s+unknown-service-prod\b.*\$4\.00\s*$")
        self.assertIn("3 distinct key names", output)
        calls = [call for call in self.calls() if call["path"] == "/v1/report"]
        self.assertEqual(calls[0]["query"].get("group_by"), ["api_key_name"])
        self.assertNotIn("api_key_id", calls[0]["query"])
        self.run_report("today")
        self.assertEqual(self.calls()[-1]["query"].get("api_key_id"), ["self"])

    def test_raw_keys_find_preserves_rank_and_team_day_is_unscoped(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "alpha-key", "total_cost": "9", "request_count": 9},
            {"api_key_name": "beta-key", "total_cost": "1", "request_count": 1}]
        output = self.run_report("users", "--keys", "--find", "beta").stdout
        self.assertIn("$10.00", output)
        self.assertRegex(output, r"(?m)^\s*2\s+beta-key\b.*\$1\.00\s*$")
        self.assertNotRegex(output, r"(?m)^\s*1\s+alpha-key")
        self.fixture["team_rows"] = [{"day": DAY, "total_cost": "8", "request_count": 2}]
        self.run_report("users", "2d", "--by", "day")
        call = self.calls()[-1]
        self.assertEqual(call["query"].get("group_by"), ["day"])
        self.assertNotIn("api_key_id", call["query"])

    def test_help_lists_all_controls_and_grouping_examples_without_discovery(self):
        marker = self.home / "security-called"
        security = self.home / "bin/security"
        security.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 44\n")
        security.chmod(0o700)
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                output = self.run_report(flag).stdout
                for term in ("today|Nd", "detail", "users", "--find TEXT",
                             "--keys", "--top N|all", "--by", "api_key_name",
                             "--from-json FILE", "inclusive UTC", "chronological",
                             "default 7d", "default all"):
                    self.assertIn(term.lower(), output.lower())
                self.assertEqual(output.count("./usage.sh users 30d --top 10"), 1)
                self.assertIn("Options for 'users':", output)
                for grouping in ("day", "model", "provider", "user", "api_key_name"):
                    self.assertIn(f"./usage.sh users 30d --by {grouping}", output)
                self.assertFalse(self.calls())
                self.assertFalse(marker.exists())

    def test_removed_team_command_rejects_without_discovery_or_network(self):
        marker = self.home / "security-called"
        security = self.home / "bin/security"
        security.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 44\n")
        security.chmod(0o700)
        result = self.run_report("team", "30d", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.calls())

    def test_all_modes_show_from_to_and_inclusive_day_count(self):
        saved = self.home / "saved.json"
        saved.write_text(json.dumps({"results": [
            {"api_key_name": "Avery Stone", "total_cost": "1", "request_count": 1}]}))
        today = datetime.now(timezone.utc).date()
        date_label = f"{today.strftime('%b')} {today.day}, {today.year}"
        expected = f"From: {date_label} · To: {date_label} · 1 day (UTC)"
        for args in (("today",), ("detail", "today"),
                     ("users", "today", "--from-json", str(saved)),
                     ("users", "today", "--from-json", str(saved))):
            with self.subTest(args=args):
                self.assertIn(expected, self.run_report(*args).stdout)
        older = today - timedelta(days=2)
        older_label = f"{older.strftime('%b')} {older.day}, {older.year}"
        output = self.run_report("users", "2d", "--from-json", str(saved)).stdout
        self.assertIn(f"From: {older_label} · To: {date_label} · 3 days (UTC)", output)
        self.assertIn("file window unconfirmed", output)

    def test_team_defaults_to_people_and_day_breakdown_is_chronological(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "lee@example.test@Lee-ABC1234567",
             "total_cost": "2", "request_count": 2}]
        output = self.run_report("users", "30d").stdout
        self.assertIn("Lee", output)
        self.assertEqual(self.calls()[-1]["query"]["group_by"], ["api_key_name"])
        older = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
        current = datetime.now(timezone.utc).date().isoformat()
        self.fixture["team_rows"] = [
            {"day": current, "total_cost": "100", "request_count": 1},
            {"day": older, "total_cost": "1", "request_count": 1},
            {"day": DAY, "total_cost": "10", "request_count": 1}]
        output = self.run_report("users", "2d", "--by", "day").stdout
        day_lines = [line for line in output.splitlines()
                     if re.match(r"^\s*\d+\s+20\d\d-\d\d-\d\d", line)]
        self.assertEqual([re.search(r"20\d\d-\d\d-\d\d", line).group()
                          for line in day_lines], [older, DAY, current], output)
        self.assertIn("$111.00", output)

    def test_known_key_forms_group_name_and_show_associated_email(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "brian@example.test@Brian-Example-D3WN9G4P69",
             "total_cost": "3", "request_count": 3},
            {"api_key_name": "[x's Brian Example ABCDEF1234] laptop",
             "total_cost": "2", "request_count": 2},
            {"api_key_name": "Brian-Example-ABCDEF1234-project",
             "total_cost": "1", "request_count": 1},
            {"api_key_name": "brian@example.test@Brian-Example-QWERTY1234-External",
             "total_cost": "1", "request_count": 1},
            {"api_key_name": "nora@contractor.example@Nora-Park-ZXCVBN1234-Andela",
             "total_cost": "4", "request_count": 4},
            {"api_key_name": "opaque-service-prod", "total_cost": "2", "request_count": 2},
            {"api_key_name": "unknown@example.test", "total_cost": "1", "request_count": 1}]
        output = self.run_report("users", "30d").stdout
        self.assertIn("$14.00", output)
        self.assertRegex(output, r"(?m)^\s*1\s+Brian Example\s+7\s+.*\$7\.00$")
        self.assertRegex(output, r"(?m)^\s*2\s+Nora Park\s+4\s+.*\$4\.00$")
        self.assertRegex(output, r"(?m)^\s*3\s+opaque-service-prod\s+2\s+.*\$2\.00$")
        self.assertRegex(output, r"(?m)^\s*4\s+unknown@example\.test\s+1\s+.*\$1\.00$")
        self.assertNotIn("full account:", output)
        self.assertNotIn("D3WN9G4P69", output)
        self.assertEqual(self.calls()[-1]["query"]["group_by"], ["api_key_name"])
        found = self.run_report("users", "--find", "brian@example.test").stdout
        self.assertIn("Brian Example", found)
        self.assertNotIn("Nora Park", found)
        raw = self.run_report("users", "--keys", "--find", "D3WN9G4P69").stdout
        self.assertIn("brian@example.test@Brian", raw)
        self.assertIn("$3.00", raw)

    def test_unknown_identity_prefers_email_and_shared_device_keys_stay_distinct(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "alice@example.test@opaque-service-prod",
             "total_cost": "4", "request_count": 4},
            {"api_key_name": "Shared-ABCDEF1234", "total_cost": "3", "request_count": 3},
            {"api_key_name": "Shared-ZXCVBN1234", "total_cost": "2", "request_count": 2}]
        output = self.run_report("users").stdout
        self.assertIn("3 people (inferred)", output)
        self.assertRegex(output, r"(?m)^\s*1\s+alice@example\.test\s+4\s+.*\$4\.00$")
        self.assertRegex(output, r"(?m)^\s*2\s+Shared-ABCDEF1234\s+3\s+.*\$3\.00$")
        self.assertRegex(output, r"(?m)^\s*3\s+Shared-ZXCVBN1234\s+2\s+.*\$2\.00$")
        self.assertNotIn("opaque-service-prod", output)

    def test_one_word_legacy_name_merges_with_plain_name(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "lee@Lee-ABC1234567", "total_cost": "2", "request_count": 2},
            {"api_key_name": "Lee", "total_cost": "1", "request_count": 1}]
        output = self.run_report("users").stdout
        self.assertIn("1 person", output)
        self.assertRegex(output, r"(?m)^\s*1\s+Lee\s+3\s+.*\$3\.00$")

    def test_team_tty_colors_title_header_rank_and_spend(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "Avery Stone", "total_cost": "99.99", "request_count": 9},
            {"api_key_name": "Blair Reed", "total_cost": "0.01", "request_count": 1}]
        self.boundary.joinpath("sitecustomize.py").write_text(
            HTTP_BOUNDARY + "\nimport sys\nsys.stdout.isatty = lambda: True\n")
        self.env.pop("NO_COLOR")
        colored = self.run_report("users").stdout
        self.assertGreaterEqual(colored.count("38;2;57;135;229m"), 5)
        self.assertRegex(colored, r"38;2;57;135;229m\$0\.01")
        self.env["NO_COLOR"] = "1"
        plain = self.run_report("users").stdout
        self.assertNotIn("\x1b[", plain)
        self.assertTrue(all(len(line) == 99 for line in plain.splitlines() if line.strip()), plain)

    def test_team_bars_use_global_top_with_fractional_blocks(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": name, "total_cost": cost, "request_count": 1}
            for name, cost in (("Top Person", "78.8"), ("Seven Person", "7.3"),
                               ("Six Person", "6.2"), ("Four Person", "4.9"),
                               ("Two Person", "2.8"))]
        output = self.run_report("users").stdout
        header = next(line for line in output.splitlines() if "VS TOP" in line)
        start = header.index("VS TOP")
        rows = [line for line in output.splitlines()
                if re.match(r"^\d+\s+(Top|Seven|Six|Four|Two) Person", line)]
        bars = [row[start:start + 17].rstrip() for row in rows]
        self.assertEqual(bars[0], "█" * 17)
        self.assertEqual(len(set(bars[1:])), 4, bars)
        self.assertTrue(any(any(char in "▏▎▍▌▋▊▉" for char in bar)
                            for bar in bars[1:]), bars)
        self.assertIn(" 7.3%", rows[1])
        found = self.run_report("users", "--find", "Two Person").stdout
        found_row = next(line for line in found.splitlines()
                         if re.match(r"^5\s+Two Person", line))
        self.assertEqual(found_row[start:start + 17].rstrip(), bars[-1])
        self.fixture["team_rows"] = [
            {"api_key_name": "Zero Person", "total_cost": "0", "request_count": 0}]
        zero = self.run_report("users").stdout
        zero_row = next(line for line in zero.splitlines()
                        if re.match(r"^1\s+Zero Person", line))
        self.assertEqual(zero_row[start:start + 17].strip(), "")

    def test_subcent_costs_keep_totals_but_have_no_bars(self):
        self.gateway_key()
        today = datetime.now(timezone.utc).date()
        amounts = ("0", "0.004", "0.009", "0.01")
        self.fixture["daily_rows"] = [
            {"day": (today - timedelta(days=offset)).isoformat(),
             "total_cost": amount, "request_count": 1}
            for offset, amount in enumerate(amounts)]
        personal = self.run_report("3d").stdout
        self.assertIn("$0.02", personal)
        for offset, amount in enumerate(amounts):
            day = (today - timedelta(days=offset)).isoformat()
            row = next(line for line in personal.splitlines() if line.lstrip().startswith(day))
            self.assertEqual("█" in row, amount == "0.01", row)
        self.fixture["daily_rows"] = [
            {"day": today.isoformat(), "total_cost": "0.004", "request_count": 1}]
        subcent = self.run_report("today").stdout
        self.assertNotIn("█" * 97, subcent)
        self.fixture["team_rows"] = [
            {"api_key_name": f"Cost {index}", "total_cost": amount, "request_count": 1}
            for index, amount in enumerate(amounts)]
        team = self.run_report("users").stdout
        self.assertIn("TOTAL: $0.02", team)
        header = next(line for line in team.splitlines() if "VS TOP" in line)
        start = header.index("VS TOP")
        for index, amount in enumerate(amounts):
            row = next(line for line in team.splitlines()
                       if re.match(rf"^\d+\s+Cost {index}\s", line))
            self.assertEqual(bool(row[start:start + 17].strip()), amount == "0.01", row)
            self.assertTrue(row.rstrip().endswith("$0.01" if amount == "0.01" else "$0.00"), row)
        self.fixture["team_rows"] = [
            {"api_key_name": "Tiny Cost", "total_cost": "0.009", "request_count": 1}]
        solo = self.run_report("users").stdout
        self.assertIn("TOTAL: $0.00", solo)
        self.assertRegex(solo, r"(?m)^1\s+Tiny Cost.*\$0\.00$")
        self.fixture["team_rows"].insert(0, {
            "api_key_name": "Large Cost", "total_cost": "0.01", "request_count": 1})
        top = self.run_report("users", "--top", "1").stdout
        self.assertIn("TOTAL: $0.02", top)
        self.assertIn("1 more people: $0.00", top)

    def test_reported_input_output_tokens_aggregate_and_survive_top_filter(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "Avery-Sample-ABCDEF1234", "total_cost": "3",
             "request_count": 2, "input_tokens": 1200, "output_tokens": 345},
            {"api_key_name": "[x's Avery Sample ZXCVBN1234] laptop", "total_cost": "2",
             "request_count": 3, "input_tokens": 800, "output_tokens": 55},
            {"api_key_name": "Blair Sample", "total_cost": "1",
             "request_count": 1, "input_tokens": 100, "output_tokens": 50},
            {"api_key_name": "Zero Sample", "total_cost": "0",
             "request_count": 1, "input_tokens": 0, "output_tokens": 0}]
        output = self.run_report("users", "--top", "1").stdout
        self.assertIn("TOTAL: $6.00 · 7 requests", output)
        self.assertIn("Tokens: input 2.1K · output 450", output)
        header = next(line for line in output.splitlines() if "INPUT" in line and "OUTPUT" in line)
        row = next(line for line in output.splitlines() if re.match(r"^1\s+Avery Sample", line))
        self.assertRegex(row, r"Avery Sample\s+5\s+2\.0K\s+400\s+83\.3%.*\$5\.00$")
        self.assertTrue(all(len(line) == 99 for line in output.splitlines() if line.strip()), output)
        found = self.run_report("users", "--find", "Zero Sample").stdout
        self.assertIn("Tokens: input 2.1K · output 450", found)
        self.assertRegex(found, r"(?m)^3\s+Zero Sample\s+1\s+0\s+0\s+0\.0%.*\$0\.00$")
        self.assertIn("INPUT", header)
        self.assertIn("OUTPUT", header)

    def test_reported_token_missing_is_unknown_and_malformed_is_error(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "Avery Sample", "total_cost": "2", "request_count": 2,
             "input_tokens": 100, "output_tokens": 50},
            {"api_key_name": "Avery Sample", "total_cost": "1", "request_count": 1,
             "output_tokens": 25},
            {"api_key_name": "Blair Sample", "total_cost": "1", "request_count": 1,
             "input_tokens": 0}]
        output = self.run_report("users").stdout
        self.assertIn("Tokens: input — · output —", output)
        self.assertRegex(output, r"(?m)^1\s+Avery Sample\s+3\s+—\s+75\s+75\.0%")
        self.assertRegex(output, r"(?m)^2\s+Blair Sample\s+1\s+0\s+—\s+25\.0%")
        for field in ("input_tokens", "output_tokens"):
            for bad in (-1, True, 1.5, "NaN", "-2", None):
                with self.subTest(field=field, bad=bad):
                    self.fixture["team_rows"][0][field] = bad
                    result = self.run_report("users", success=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("TOTAL:", result.stdout)
                    self.fixture["team_rows"][0][field] = 100 if field == "input_tokens" else 50

    def test_team_top_preserves_total_and_rejects_invalid_values(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "a", "total_cost": "9", "request_count": 9},
            {"api_key_name": "b", "total_cost": "1", "request_count": 1}]
        output = self.run_report("users", "--top", "1").stdout
        self.assertIn("TOTAL", output)
        self.assertIn("$10.00", output)
        self.assertIn("1 more", output)
        for option in ("0", "-1", "bad"):
            self.assertNotEqual(self.run_report("users", "--top", option,
                                                success=False).returncode, 0)
        for bad in ("NaN", "Infinity", "-1", True, None):
            with self.subTest(bad=bad):
                self.fixture["team_rows"][0]["total_cost"] = bad
                result = self.run_report("users", success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("$10.00", result.stdout)
        self.fixture["team_rows"][0]["total_cost"] = "9"
        self.fixture["team_rows"][0]["request_count"] = "bad"
        result = self.run_report("users", success=False)
        self.assertNotEqual(result.returncode, 0)

    def test_offline_team_json_skips_discovery_and_network(self):
        saved = self.home / "saved.json"
        saved.write_text(json.dumps({"results": [
            {"api_key_name": "saved-key", "total_cost": "3.00", "request_count": 2}]}))
        marker = self.home / "security-called"
        security = self.home / "bin/security"
        security.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 44\n")
        security.chmod(0o700)
        result = self.run_report("users", "--from-json", str(saved))
        self.assertIn("$3.00", result.stdout)
        self.assertIn("saved", result.stdout.lower())
        self.assertNotIn("verified", result.stdout.lower())
        self.assertFalse(self.calls())
        self.assertFalse(marker.exists())
        self.assertFalse(list(self.state.rglob("*.sqlite3")))

    def test_empty_saved_path_fails_before_credential_discovery(self):
        marker = self.home / "security-called"
        security = self.home / "bin/security"
        security.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 44\n")
        security.chmod(0o700)
        result = self.run_report("users", "--from-json", "", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.calls())

    def test_quiet_absent_providers_and_incomplete_configured_failure(self):
        plain = self.run_report("today").stdout
        self.assertNotRegex(plain, r"(?i)Claude|Codex|OpenAI|gateway key not configured")
        detail = self.run_report("detail", "today").stdout
        self.assertNotRegex(detail, r"(?m)^CLAUDE|^CODEX|no saved usage")
        self.gateway_key()
        self.openai_key()
        self.fixture["openai_error"] = True
        output = self.run_report("30d").stdout
        self.assertIn("$2.50", output)
        self.assertIn("TOTAL · VERCEL BILLED", output)
        self.assertRegex(output, r"(?m)^TOTAL.*\$2\.50$")

    def test_team_zero_unicode_control_and_long_labels_fit_99_columns(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "山田 太郎 " + "長" * 22 + "\x1b[31m",
             "total_cost": "0", "request_count": 0},
            {"api_key_name": "cafe\u0301-service", "total_cost": "0", "request_count": 1}]
        output = self.run_report("users", "--keys").stdout
        self.assertIn("TOTAL: $0.00", output)
        self.assertIn("cafe\u0301-service", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("full account:", output)
        widths = {sum(0 if __import__("unicodedata").combining(char) else
                      2 if __import__("unicodedata").east_asian_width(char) in ("F", "W")
                      else 1 for char in line)
                  for line in output.splitlines() if line.strip()}
        self.assertEqual(widths, {99}, output)

    def test_team_grouping_validation_and_dimension_query(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"provider": "anthropic", "total_cost": "1.25", "request_count": 2}]
        result = self.run_report("users", "2d", "--by", "provider")
        self.assertIn("anthropic", result.stdout)
        self.assertEqual(self.calls()[-1]["query"]["group_by"], ["provider"])
        for row in ({"provider": None, "total_cost": 1, "request_count": 1},
                    {"provider": "a", "total_cost": 1, "request_count": None},
                    {"provider": "a", "total_cost": 1, "request_count": -1}):
            self.fixture["team_rows"] = [row]
            self.assertNotEqual(self.run_report("users", "--by", "provider",
                                                success=False).returncode, 0)

    def test_team_empty_report_and_unnamed_key_keep_valid_charge(self):
        self.gateway_key()
        self.fixture["team_rows"] = []
        output = self.run_report("users").stdout
        self.assertIn("TOTAL: $0.00", output)
        self.assertNotIn("Traceback", output)
        self.fixture["team_rows"] = [
            {"api_key_name": None, "total_cost": "1.25", "request_count": 2}]
        output = self.run_report("users").stdout
        self.assertIn("(unnamed key)", output)
        self.assertIn("$1.25", output)

    def test_team_user_dimension_accepts_opaque_identifiers(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"user": "opaque-user-a", "total_cost": "1.00", "request_count": 1},
            {"user": "opaque-user-b", "total_cost": "2.00", "request_count": 2}]
        output = self.run_report("users", "--by", "user").stdout
        self.assertIn("opaque-user-a", output)
        self.assertIn("opaque-user-b", output)
        self.assertIn("$3.00", output)

    def test_keys_rejects_other_groupings_before_discovery(self):
        marker = self.home / "security-called"
        security = self.home / "bin/security"
        security.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 44\n")
        security.chmod(0o700)
        for grouping in ("day", "model", "provider", "user"):
            for args in (("--keys", "--by", grouping), ("--by", grouping, "--keys")):
                with self.subTest(args=args):
                    result = self.run_report("users", *args, success=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("--keys requires --by api_key_name", result.stderr)
                    self.assertIn("Usage", result.stderr)
                    self.assertFalse(marker.exists())
                    self.assertFalse(self.calls())
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "raw-key", "total_cost": "1", "request_count": 1}]
        self.assertIn("raw-key", self.run_report("users", "--keys").stdout)
        self.assertIn("raw-key", self.run_report(
            "users", "--by", "api_key_name", "--keys").stdout)

    def test_user_json_session_id_is_validated_before_filtering(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"user": json.dumps({"account_uuid": "account-fixture", "session_id": 123}),
             "total_cost": "1", "request_count": 1}]
        for args in (("users", "--by", "user"),
                     ("users", "--by", "user", "--find", "123")):
            with self.subTest(args=args):
                result = self.run_report(*args, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Gateway report unavailable: invalid Gateway user", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        for session in (None, "", "session-fixture"):
            with self.subTest(session=session):
                self.fixture["team_rows"][0]["user"] = json.dumps(
                    {"account_uuid": "account-fixture", "session_id": session})
                args = ("--find", "session-fixture") if session else ()
                output = self.run_report("users", "--by", "user", *args).stdout
                self.assertIn("account-fixture", output)

    def test_user_json_without_account_retains_charges_and_session_filter(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"user": json.dumps({"account_uuid": "account-fixture", "session_id": "known"}),
             "total_cost": "1", "request_count": 1},
            {"user": json.dumps({"session_id": "session-fixture"}),
             "total_cost": "2", "request_count": 2},
            {"user": json.dumps({}), "total_cost": "3", "request_count": 3},
            {"user": json.dumps({"account_uuid": None, "device_id": ""}),
             "total_cost": "4", "request_count": 4}]
        output = self.run_report("users", "--by", "user").stdout
        self.assertIn("TOTAL: $10.00 · 10 requests", output)
        self.assertRegex(output, r"(?m)^\s*1\s+\(unknown user\).*\$9\.00\s*$")
        self.assertRegex(output, r"(?m)^\s*2\s+account-fixture.*\$1\.00\s*$")
        filtered = self.run_report("users", "--by", "user", "--find", "session-fixture").stdout
        self.assertIn("TOTAL: $10.00 · 10 requests", filtered)
        self.assertIn("(unknown user)", filtered)
        self.assertNotIn("account-fixture", filtered)
        for identity in (123, False):
            with self.subTest(identity=identity):
                self.fixture["team_rows"][1]["user"] = json.dumps(
                    {"account_uuid": identity, "session_id": "session-fixture"})
                result = self.run_report("users", "--by", "user", success=False)
                self.assertIn("Gateway report unavailable: invalid Gateway user", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_team_rejects_missing_option_values(self):
        for option in ("--find", "--by", "--top", "--from-json"):
            with self.subTest(option=option):
                result = self.run_report("users", option, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Usage", result.stderr)

    def test_team_large_request_counts_keep_spend_aligned(self):
        self.gateway_key()
        self.fixture["team_rows"] = [
            {"api_key_name": "large", "total_cost": "1.00",
             "request_count": 1000000000000}]
        output = self.run_report("users").stdout
        row = next(line for line in output.splitlines() if "large" in line)
        self.assertIn("1,000,000,000,000", row)
        self.assertEqual(len(row), 99)
        self.assertTrue(row.endswith("$1.00"))

    def test_long_unknown_key_name_is_preserved_across_display_lines(self):
        self.gateway_key()
        name = "opaque-" + "長" * 60 + "-suffix"
        self.fixture["team_rows"] = [
            {"api_key_name": name, "total_cost": "1", "request_count": 1}]
        output = self.run_report("users", "--keys").stdout
        self.assertNotIn("full account:", output)
        self.assertIn("opaque-", output)
        self.assertIn("…", output)
        self.assertIn("$1.00", self.run_report("users", "--keys", "--find", name).stdout)

    def test_gateway_keychain_fallback_with_native_environment(self):
        security = self.home / "bin/security"
        security.write_text("#!/bin/sh\nprintf '%s' 'fixture-gateway-secret'\n")
        security.chmod(0o700)
        self.env.update({"ANTHROPIC_API_KEY": "sk-ant-private-native",
                         "USAGE_TEST_TOKEN": "fixture-gateway-secret"})
        result = self.run_report("30d")
        self.assertIn("$2.50", result.stdout)
        gateway = [call for call in self.calls() if call["host"] == "ai-gateway.vercel.sh"]
        self.assertTrue(gateway)
        self.assertTrue(all(call["expected_auth"] for call in gateway))

    def test_empty_gateway_helper_falls_through_to_keychain(self):
        self.claude.mkdir()
        (self.claude / "settings.json").write_text(json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh/claude-code"},
            "apiKeyHelper": "true"}))
        security = self.home / "bin/security"
        security.write_text("#!/bin/sh\nprintf '%s' 'fixture-gateway-secret'\n")
        security.chmod(0o700)
        self.env["USAGE_TEST_TOKEN"] = "fixture-gateway-secret"
        output = self.run_report("30d").stdout
        self.assertIn("$2.50", output)
        self.assertTrue(all(call["expected_auth"] for call in self.calls()
                            if call["host"] == "ai-gateway.vercel.sh"))

    def test_excessive_period_fails_without_traceback_or_discovery(self):
        marker = self.home / "security-called"
        security = self.home / "bin/security"
        security.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 44\n")
        security.chmod(0o700)
        result = self.run_report("users", "999999999999999999d", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(marker.exists())

    def test_gateway_keychain_service_is_configurable_and_never_native(self):
        security = self.home / "bin/security"
        calls = self.home / "security-calls"
        security.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$USAGE_TEST_SECURITY_CALLS\"\n"
                            "printf '%s' 'fixture-gateway-secret'\n")
        security.chmod(0o700)
        self.env["USAGE_TEST_SECURITY_CALLS"] = str(calls)
        self.env["AI_GATEWAY_KEYCHAIN_SERVICE"] = "fixture-gateway"
        self.env["USAGE_TEST_TOKEN"] = "fixture-gateway-secret"
        self.assertIn("$2.50", self.run_report("30d").stdout)
        self.assertIn("-s fixture-gateway", calls.read_text())
        self.assertTrue(all(call["host"] == "ai-gateway.vercel.sh" for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
