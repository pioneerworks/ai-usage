#!/usr/bin/env bash
set -euo pipefail

exec python3 - "$@" <<'PYTHON'
import json
import hashlib
import math
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

GATEWAY = "https://ai-gateway.vercel.sh"
WIDTH = 99
USAGE = ("Usage: usage.sh [today|Nd] | detail [today|Nd] | "
         "users [today|Nd] [--find TEXT] [--keys] "
         "[--by day|model|provider|user|api_key_name] "
         "[--top N|all] [--from-json FILE] | --help")


def arguments(args):
    if args and args[0] in ("--help", "-h"):
        print("Usage:")
        print("  ./usage.sh [today|Nd]         Personal provider-reported charges (default 7d)")
        print("  ./usage.sh detail [today|Nd]  Provider charges and separate local estimates")
        print("  ./usage.sh users [today|Nd]   Gateway people ranked by spend")
        print()
        print("Options for 'users':")
        print("  --find TEXT          Match labels, names, emails, or key names; keep overall rank")
        print("  --keys               Group raw key names, not secret key values")
        print("  --top N|all          Show N rows or all (default all)")
        print("  --by day|model|provider|user|api_key_name")
        print("                       Group Gateway charges; --by day is chronological")
        print("  --from-json FILE     Render a saved Gateway report offline with the selected grouping")
        print("  -h, --help           Show this help")
        print()
        print("Periods use inclusive UTC dates; today or Nd, default 7d.")
        print("Example: ./usage.sh users 30d --top 10")
        print()
        print("Grouping examples:")
        print("  ./usage.sh users 30d --by day           # Daily totals")
        print("  ./usage.sh users 30d --by model         # Spend by model")
        print("  ./usage.sh users 30d --by provider      # Spend by provider")
        print("  ./usage.sh users 30d --by user          # Gateway user IDs")
        print("  ./usage.sh users 30d --by api_key_name  # People inferred from key names (default)")
        sys.exit(0)
    mode = args[0] if args and args[0] in ("detail", "users") else "personal"
    if mode != "personal":
        args = args[1:]
    period = "7d"
    options = {"by": "api_key_name",
               "top": None, "keys": False, "find": None, "from_json": None}
    seen_period = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "today" or re.fullmatch(r"[1-9]\d*d", arg):
            if seen_period:
                break
            period, seen_period = arg, True
        elif mode == "users" and arg == "--keys":
            options["keys"] = True
        elif mode == "users" and arg in ("--by", "--find", "--top", "--from-json"):
            i += 1
            if i >= len(args):
                print(USAGE, file=sys.stderr)
                sys.exit(2)
            value = args[i]
            if arg == "--by":
                if value not in ("day", "model", "provider", "user", "api_key_name"):
                    break
                options["by"] = value
            elif arg == "--find":
                options["find"] = value.casefold()
            elif arg == "--top":
                if value != "all" and not re.fullmatch(r"[1-9]\d*", value):
                    break
                options["top"] = None if value == "all" else int(value)
            else:
                if not value:
                    print(USAGE, file=sys.stderr)
                    sys.exit(2)
                options["from_json"] = value
        else:
            break
        i += 1
    if i != len(args):
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    if options["keys"] and options["by"] != "api_key_name":
        print("--keys requires --by api_key_name", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    return mode, period, options


mode, period, options = arguments(sys.argv[1:])
detail = mode == "detail"
now = datetime.now().astimezone()
today = datetime.now(timezone.utc).date()
try:
    cutoff_day = today if period == "today" else today - timedelta(days=int(period[:-1]))
except (OverflowError, ValueError):
    print(USAGE, file=sys.stderr)
    sys.exit(2)
start_date, end_date = cutoff_day.isoformat(), today.isoformat()
day_count = (today - cutoff_day).days + 1
from_label = f"{cutoff_day.strftime('%b')} {cutoff_day.day}, {cutoff_day.year}"
to_label = f"{today.strftime('%b')} {today.day}, {today.year}"
window_label = (f"From: {from_label} · To: {to_label} · "
                f"{day_count} {'day' if day_count == 1 else 'days'} (UTC)")

claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
identity_path = (claude_dir / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR")
                 else Path.home() / ".claude.json")
codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state").expanduser() / "ai-usage"


def gateway_token():
    explicit = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
    if explicit:
        return "" if explicit.lower().startswith("sk-ant-") else explicit
    try:
        settings = json.loads((claude_dir / "settings.json").read_text())
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    settings_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    env_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    env_credential_present = any(os.environ.get(key, "").strip() for key in
                                 ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    if env_base:
        base_url = env_base
        credential_env = os.environ
        helper = None
    elif env_credential_present:
        base_url = ""
        credential_env = {}
        helper = None
    else:
        base_url = settings_env.get("ANTHROPIC_BASE_URL", "")
        credential_env = settings_env
        helper = settings.get("apiKeyHelper")
    try:
        gateway_host = urllib.parse.urlparse(base_url).hostname
    except (TypeError, ValueError):
        gateway_host = None
    if gateway_host == "ai-gateway.vercel.sh":
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            credential = credential_env.get(key)
            if isinstance(credential, str) and credential.strip():
                credential = credential.strip()
                if not credential.lower().startswith("sk-ant-"):
                    return credential
        if isinstance(helper, str) and helper.strip():
            try:
                result = subprocess.run(helper, shell=True, capture_output=True, text=True,
                                        timeout=5, check=False)
                if result.returncode == 0:
                    credential = result.stdout.strip()
                    if credential and not credential.lower().startswith("sk-ant-"):
                        return credential
            except (OSError, subprocess.TimeoutExpired):
                pass
    service = os.environ.get("AI_GATEWAY_KEYCHAIN_SERVICE") or "pioneerworks-ai-gateway"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=5, check=False)
        credential = result.stdout.strip()
        if result.returncode == 0 and credential and not credential.lower().startswith("sk-ant-"):
            return credential
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


token = "" if options["from_json"] else gateway_token()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def get_json(path, authenticated=False):
    headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
    request = urllib.request.Request(GATEWAY + path, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        return json.load(response)


def money(amount, estimate=False):
    rendered = "$0.00" if 0 <= Decimal(str(amount)) < Decimal("0.01") else f"${amount:,.2f}"
    return ("≈" if estimate else "") + rendered


def short(amount):
    if amount >= 1_000_000_000:
        return f"{Decimal(str(amount)) / Decimal(1_000_000_000):.1f}B"
    if amount >= 1_000_000:
        return f"{Decimal(str(amount)) / Decimal(1_000_000):.1f}M"
    if amount >= 1_000:
        return f"{Decimal(str(amount)) / Decimal(1_000):.1f}K"
    return str(int(amount))


def team_person(key_name):
    non_person = {"shared", "service", "internal", "bot", "ci"}
    bracket = re.match(r"^\[[^']*'s (.+?) [A-Z0-9]{8,}\](?: |$)", key_name)
    if bracket and bracket.group(1).split()[0].casefold() not in non_person:
        return bracket.group(1), ""
    prefix, separator, identity = key_name.rpartition("@")
    if not separator:
        identity = key_name
    email = (prefix if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", prefix) else "")
    named = re.fullmatch(
        r"([A-Z][A-Za-z]*(?:-[A-Z][A-Za-z]*)*)-[A-Z0-9]{8,}"
        r"(?:-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?", identity)
    if named and named.group(1).split("-")[0].casefold() not in non_person:
        return named.group(1).replace("-", " "), email
    if email:
        return email, email
    if re.fullmatch(r"[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)*", key_name):
        return key_name, ""
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", key_name):
        return key_name, key_name
    return key_name, email


def team_text(value):
    value = "".join(char for char in value if unicodedata.category(char) != "Cc"
                    and unicodedata.category(char) != "Cf")
    return " ".join(value.split())


def team_width(value):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
               for char in value)


def team_fit(value, width):
    if team_width(value) <= width:
        return value + " " * (width - team_width(value))
    result = ""
    for char in value:
        if team_width(result + char + "…") > width:
            break
        result += char
    result += "…"
    return result + " " * (width - team_width(result))


def team_chunks(value, width):
    start = 0
    used = 0
    for index, char in enumerate(value):
        size = team_width(char)
        if used + size > width:
            yield value[start:index]
            start, used = index, 0
        used += size
    yield value[start:]


def team_report():
    by = options["by"]
    saved = options["from_json"]
    if saved:
        with open(saved, encoding="utf-8") as stream:
            response = json.load(stream)
    else:
        if not token:
            raise ValueError("Gateway credential unavailable; set AI_GATEWAY_API_KEY")
        query = urllib.parse.urlencode({"start_date": start_date, "end_date": end_date,
                                        "group_by": by})
        response = get_json("/v1/report?" + query, authenticated=True)
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise ValueError("invalid Gateway report: results must be a list")
    buckets = {}
    token_fields_seen = False
    for row in response["results"]:
        if not isinstance(row, dict):
            raise ValueError("invalid Gateway report row")
        raw_cost = row.get("total_cost")
        if isinstance(raw_cost, bool) or not isinstance(raw_cost, (str, int, float)):
            raise ValueError("invalid Gateway cost")
        try:
            cost = Decimal(str(raw_cost))
        except InvalidOperation:
            raise ValueError("invalid Gateway cost")
        if not cost.is_finite() or cost < 0:
            raise ValueError("invalid Gateway cost")
        raw_count = row.get("request_count")
        if isinstance(raw_count, bool) or not (
                isinstance(raw_count, int) or
                isinstance(raw_count, str) and re.fullmatch(r"\d+", raw_count)):
            raise ValueError("invalid Gateway request count")
        count = int(raw_count)
        if count < 0:
            raise ValueError("invalid Gateway request count")
        row_tokens = []
        for field in ("input_tokens", "output_tokens"):
            if field not in row:
                row_tokens.append(None)
                continue
            token_fields_seen = True
            raw_tokens = row[field]
            if isinstance(raw_tokens, bool) or not (
                    isinstance(raw_tokens, int) or
                    isinstance(raw_tokens, str) and re.fullmatch(r"\d+", raw_tokens)):
                raise ValueError(f"invalid Gateway {field}")
            parsed_tokens = int(raw_tokens)
            if parsed_tokens < 0:
                raise ValueError(f"invalid Gateway {field}")
            row_tokens.append(parsed_tokens)
        raw = row.get(by)
        if by == "api_key_name" and (raw is None or raw == ""):
            raw = "(unnamed key)"
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("invalid Gateway grouping label")
        if by == "day":
            try:
                if datetime.strptime(raw, "%Y-%m-%d").date().isoformat() != raw:
                    raise ValueError("invalid Gateway day")
            except ValueError:
                raise ValueError("invalid Gateway day")
            if not saved and not start_date <= raw <= end_date:
                raise ValueError("Gateway day outside requested window")
        email = ""
        if by == "user":
            label, subkey = raw, raw
            if raw.startswith("{"):
                try:
                    user = json.loads(raw)
                except ValueError:
                    raise ValueError("invalid Gateway user")
                if not isinstance(user, dict):
                    raise ValueError("invalid Gateway user")
                identities = (user.get("account_uuid"), user.get("device_id"))
                if any(identity is not None and not isinstance(identity, str)
                       for identity in identities):
                    raise ValueError("invalid Gateway user")
                label = next((identity for identity in identities
                              if identity and identity.strip()), "(unknown user)")
                session_id = user.get("session_id")
                if session_id is None or session_id == "":
                    subkey = raw
                elif isinstance(session_id, str):
                    subkey = session_id
                else:
                    raise ValueError("invalid Gateway user")
        elif by == "api_key_name":
            person, email = team_person(raw)
            label = raw if options["keys"] else person
            subkey = raw
        else:
            label, subkey = raw, raw
        label = team_text(label)
        email = team_text(email)
        if not label:
            raise ValueError("invalid Gateway grouping label")
        bucket = buckets.setdefault(label, [Decimal(0), 0, set(), set(), 0, 0, False, False])
        bucket[0] += cost
        bucket[1] += count
        bucket[2].add(subkey)
        if email:
            bucket[3].add(email)
        for index, value in enumerate(row_tokens):
            if value is None:
                bucket[6 + index] = True
            else:
                bucket[4 + index] += value
    total = sum((values[0] for values in buckets.values()), Decimal(0))
    total_requests = sum(values[1] for values in buckets.values())
    if by == "day":
        items = sorted(buckets.items())
    else:
        items = sorted(buckets.items(), key=lambda item: (-item[1][0], item[0].casefold(),
                                                           item[0]))
    ranked = [(rank, label, values) for rank, (label, values) in enumerate(items, 1)]
    find = options["find"]
    display = ([item for item in ranked if find in item[1].casefold()
                or any(find in name.casefold() for name in item[2][2] | item[2][3])]
               if find else ranked)
    top = options["top"]
    shown = display[:top] if top is not None else display
    unit = ("key" if options["keys"] else "person") if by == "api_key_name" else by
    plural = "people" if unit == "person" else unit + "s"
    rank_width = max(4, len(str(len(items))))
    requests_width = max([10] + [len(f"{values[1]:,}") for _, values in items])
    cost_width = max([13] + [len(money(values[0])) for _, values in items])
    input_width = max([7] + [len("—" if values[6] else short(values[4]))
                             for _, values in items]) if token_fields_seen else 0
    output_width = max([7] + [len("—" if values[7] else short(values[5]))
                              for _, values in items]) if token_fields_seen else 0
    extra = ((rank_width - 4) + (requests_width - 10) + (cost_width - 13) +
             (input_width - 7 if token_fields_seen else 0) +
             (output_width - 7 if token_fields_seen else 0))
    bar_width = max(0, 17 - extra)
    name_width = (27 if token_fields_seen else 43) - max(0, extra - 17)
    if name_width < 12 or cost_width > 25 or len(money(total)) > 60:
        raise ValueError("Gateway report values too large to display")
    inferred = " (inferred)" if by == "api_key_name" and not options["keys"] else ""
    count_label = unit if len(items) == 1 else plural
    summary = (f"TOTAL: {money(total)} · {total_requests:,} requests · "
               f"{len(items)} {count_label}{inferred}")
    top_cost = max((values[0] for _, values in items), default=Decimal(0))
    if team_width(summary) > WIDTH:
        raise ValueError("Gateway report values too large to display")
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    def blue(value, bold=False):
        if not use_color:
            return value
        style = "1;38" if bold else "38"
        return f"\x1b[{style};2;57;135;229m{value}\x1b[0m"
    def share(cost):
        return float(cost / total * 100) if total else 0.0
    def line(value="", accent=False):
        if not value:
            print()
            return
        if team_width(value) > WIDTH:
            raise ValueError("team line exceeds display width")
        rendered = value + " " * (WIDTH - team_width(value))
        print(blue(rendered, True) if accent else rendered)
    line()
    if by == "api_key_name":
        kind = "key" if options["keys"] else "user"
        title = (f"Saved Gateway {kind} report · by spend" if saved else
                 f"AI Gateway {kind} ranking · by spend")
    else:
        title = f"{'Saved Gateway' if saved else 'AI Gateway'} usage · by {by}"
    line(title, accent=True)
    if saved:
        for index, piece in enumerate(team_chunks(team_text(saved), WIDTH - 14)):
            line(("Saved source: " if index == 0 else " " * 14) + piece)
        line(f"Selected query window · {window_label}")
        line("Saved file window unconfirmed.")
    else:
        line(f"{window_label} · generated {now.strftime('%Y-%m-%d %H:%M %Z')}")
    line("=" * WIDTH, accent=True)
    line(summary, accent=True)
    if token_fields_seen:
        input_total = ("—" if any(values[6] for _, values in items) else
                       short(sum(values[4] for _, values in items)))
        output_total = ("—" if any(values[7] for _, values in items) else
                        short(sum(values[5] for _, values in items)))
        line(f"Tokens: input {input_total} · output {output_total}", accent=True)
    if by == "api_key_name" and not options["keys"]:
        distinct = len({name for _, values in items for name in values[2]})
        line(f"Grouped from {distinct} distinct key names; shared keys can represent multiple people.")
    if find:
        line(f"Matching {len(display)} of {len(items)} {plural}")
    line()
    heading = {"api_key_name": "KEY" if options["keys"] else "PERSON",
               "day": "DAY", "model": "MODEL", "provider": "PROVIDER",
               "user": "USER"}[by]
    bar_heading = "VS TOP"[:bar_width].ljust(bar_width)
    token_heading = (f" {'INPUT':>{input_width}} {'OUTPUT':>{output_width}}"
                     if token_fields_seen else "")
    header = (f"{'#':<{rank_width}} {heading:<{name_width}} "
              f"{'REQUESTS':>{requests_width}}{token_heading} {'SHARE':>7} "
              f"{bar_heading} {'SPEND':>{cost_width}}")
    line(header, accent=True)
    line("-" * WIDTH)
    for rank, account, values in shown:
        cost, requests = values[:2]
        rank_cell = f"{rank:<{rank_width}}"
        name_cell = team_fit(account, name_width)
        request_cell = f"{requests:>{requests_width},}"
        token_cells = (f" {'—' if values[6] else short(values[4]):>{input_width}} "
                       f"{'—' if values[7] else short(values[5]):>{output_width}}"
                       if token_fields_seen else "")
        share_cell = f"{share(cost):>6.1f}%"
        bar_steps = (min(bar_width * 8, round(cost / top_cost * bar_width * 8))
                     if top_cost and cost >= Decimal("0.01") else 0)
        full_blocks, fraction = divmod(bar_steps, 8)
        bar_cell = ("█" * full_blocks + ("▏▎▍▌▋▊▉"[fraction - 1] if fraction else ""))
        bar_cell = bar_cell.ljust(bar_width)
        cost_text = money(cost)
        cost_padding = " " * (cost_width - len(cost_text))
        cost_cell = cost_padding + cost_text
        plain = (f"{rank_cell} {name_cell} {request_cell}{token_cells} {share_cell} "
                 f"{bar_cell} {cost_cell}")
        if team_width(plain) != WIDTH:
            raise ValueError("team row exceeds display width")
        if use_color:
            print(f"{blue(rank_cell)} {name_cell} {request_cell}{token_cells} {share_cell} "
                  f"{blue(bar_cell) if bar_steps else bar_cell} "
                  f"{cost_padding}{blue(cost_text)}")
        else:
            print(plain)
    if len(display) > len(shown):
        tail = sum((entry[2][0] for entry in display[len(shown):]), Decimal(0))
        line(f"  … {len(display) - len(shown)} more {plural}: "
             f"{money(tail)} ({share(tail):.1f}%)")
    line()


if mode == "users":
    try:
        team_report()
    except (OSError, ValueError, TypeError, InvalidOperation) as error:
        print(f"Gateway report unavailable: {error}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


try:
    catalog = get_json("/v1/models") if detail else {"data": []}
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        raise ValueError("invalid catalog")
    catalog_unavailable = False
except (OSError, ValueError, TypeError):
    catalog = {"data": []}
    catalog_unavailable = True

raw_prices = {}
prices = {}
for item in catalog["data"]:
    if not isinstance(item, dict) or not isinstance(item.get("pricing"), dict):
        continue
    model, pricing = item.get("id"), item["pricing"]
    if not isinstance(model, str):
        continue
    try:
        input_rate = float(pricing.get("input") or 0)
        prices[model] = {
            "input": input_rate,
            "output": float(pricing.get("output") or 0),
            "cache_read": float(pricing.get("input_cache_read") or 0),
            "cache_write": float(pricing.get("input_cache_write") or input_rate),
        }
        raw_prices[model] = pricing
    except (TypeError, ValueError):
        continue


def canonical_model(model):
    if model.startswith("claude-code/"):
        model = model[len("claude-code/"):]
    model = re.sub(r"-\d{8}$", "", model)
    return re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)


def resolve_price(model):
    for name in dict.fromkeys((model, canonical_model(model))):
        for candidate in (name, "anthropic/" + name, "openai/" + name):
            if candidate in prices:
                return prices[candidate]
    return None


def claude_cost(model, tokens):
    price = resolve_price(model)
    if price is None:
        return None
    inp, out, cache_read, cache_write, cache_write_1h = tokens
    return (inp * price["input"] + out * price["output"]
            + cache_read * price["cache_read"]
            + (cache_write - cache_write_1h) * price["cache_write"]
            + cache_write_1h * 2 * price["input"])


def tier_rate(pricing, key, prompt_tokens):
    for tier in pricing.get(key + "_tiers") or []:
        if prompt_tokens >= tier.get("min", 0) and (
                "max" not in tier or prompt_tokens < tier["max"]):
            return float(tier["cost"])
    return float(pricing.get(key) or 0)


def codex_cost(model, usage):
    pricing = raw_prices.get("openai/" + model) or raw_prices.get(model)
    if pricing is None:
        return None
    prompt = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cached_input_tokens", 0) or 0
    cache_write = usage.get("cache_write_input_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    input_only = max(prompt - cache_read - cache_write, 0)
    cache_write_key = "input_cache_write" if pricing.get("input_cache_write") else "input"
    return (input_only * tier_rate(pricing, "input", prompt)
            + cache_read * tier_rate(pricing, "input_cache_read", prompt)
            + cache_write * tier_rate(pricing, cache_write_key, prompt)
            + output * tier_rate(pricing, "output", prompt))


db = None
if detail:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(state_dir / "usage-v1.sqlite3", timeout=0.2)
        db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, signature TEXT, entries TEXT)")
    except (OSError, sqlite3.Error):
        if db is not None:
            db.close()
        db = None


def usage_entries(path, source):
    global db
    before = path.stat()
    cache_version = 2 if source == "codex" else 1
    signature = json.dumps([cache_version, source, before.st_mtime_ns, before.st_ctime_ns,
                            before.st_size, before.st_ino])
    if db is not None:
        try:
            row = db.execute("SELECT entries FROM files WHERE path=? AND signature=?",
                             (str(path), signature)).fetchone()
            if row is not None:
                cached = json.loads(row[0])
                if isinstance(cached, list):
                    return cached
        except (sqlite3.Error, ValueError):
            db.close()
            db = None
    entries = []
    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream):
            if source == "claude" and b'"assistant"' not in raw:
                continue
            if source == "codex" and line_number and b'"token_count"' not in raw and b'"turn_context"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                if source == "codex" and line_number == 0:
                    break
                continue
            if source == "claude":
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message") or {}
                entries.append({"timestamp": entry.get("timestamp"), "uuid": entry.get("uuid"),
                                "message": {key: message.get(key) for key in ("id", "model", "usage")}})
            else:
                payload = entry.get("payload") or {}
                entries.append({"type": entry.get("type"), "timestamp": entry.get("timestamp"),
                                "payload": {key: payload.get(key) for key in
                                            ("id", "model_provider", "model", "type", "info")}})
                if line_number == 0 and (entry.get("type") != "session_meta" or
                                         payload.get("model_provider") != "openai"):
                    break
    after = path.stat()
    after_signature = json.dumps([cache_version, source, after.st_mtime_ns, after.st_ctime_ns,
                                  after.st_size, after.st_ino])
    if db is not None and signature == after_signature:
        try:
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)",
                       (str(path), signature, json.dumps(entries, separators=(",", ":"))))
            db.commit()
        except sqlite3.Error:
            db.close()
            db = None
    return entries


def timestamp(value):
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


claude_messages = {}
projects = claude_dir / "projects"
if detail and projects.exists():
    for path in projects.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date() < cutoff_day:
                continue
            for entry in usage_entries(path, "claude"):
                dt = timestamp(entry.get("timestamp"))
                if dt is None or dt.date() < cutoff_day:
                    continue
                message = entry.get("message") or {}
                model = message.get("model") or "unknown"
                usage = message.get("usage") or {}
                if model == "<synthetic>" or not usage:
                    continue
                msg_id = message.get("id") or f"{path}:{entry.get('uuid')}"
                tokens = tuple(usage.get(key, 0) or 0 for key in
                               ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens"))
                cache_1h = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0
                size = sum(tokens)
                previous = claude_messages.get(msg_id)
                if previous is None or size >= previous["size"]:
                    claude_messages[msg_id] = {
                        "model": model, "datetime": dt, "tokens": (*tokens, cache_1h),
                        "size": size, "advisors": [item for item in usage.get("iterations") or []
                                                    if item.get("type") == "advisor_message"],
                    }
        except OSError:
            continue

codex_requests = []
seen_codex_sessions = set()
for root in ((codex_dir / "sessions", codex_dir / "archived_sessions") if detail else ()):
    if not root.exists():
        continue
    for path in root.rglob("rollout-*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date() < cutoff_day:
                continue
            entries = iter(usage_entries(path, "codex"))
            meta = next(entries, {})
            if meta.get("type") != "session_meta" or (meta.get("payload") or {}).get("model_provider") != "openai":
                continue
            session_id = (meta.get("payload") or {}).get("id")
            session_key = (("id", session_id) if isinstance(session_id, str) and session_id
                           else ("filename", path.name))
            if session_key in seen_codex_sessions:
                continue
            session_requests = []
            model, previous_total = "unknown", None
            for entry in entries:
                payload = entry.get("payload") or {}
                if entry.get("type") == "turn_context":
                    model = payload.get("model") or model
                    continue
                info = payload.get("info")
                if payload.get("type") != "token_count" or not info:
                    continue
                total = info.get("total_token_usage")
                if total == previous_total:
                    continue
                previous_total = total
                dt = timestamp(entry.get("timestamp"))
                if dt is not None and dt.date() >= cutoff_day:
                    session_requests.append((model, dt, info.get("last_token_usage") or {}))
            if session_requests:
                codex_requests.extend(session_requests)
                seen_codex_sessions.add(session_key)
        except OSError:
            continue
if db is not None:
    db.close()

gateway_state = "Gateway not configured"
gateway_models = {}
gateway_days = {}
gateway_total = None
gateway_requests = 0
if token:
    try:
        path = "/v1/report?" + urllib.parse.urlencode({
            "start_date": start_date, "end_date": end_date,
            "group_by": "day", "api_key_id": "self"})
        response = get_json(path, authenticated=True)
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ValueError("invalid gateway report")
        for row in response["results"]:
            if not isinstance(row, dict):
                raise ValueError("invalid gateway row")
            day = row["day"]
            if isinstance(row["total_cost"], bool):
                raise ValueError("invalid gateway daily cost")
            cost = float(row["total_cost"])
            if (not isinstance(day, str) or datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day
                    or not start_date <= day <= end_date or not math.isfinite(cost)
                    or cost < 0):
                raise ValueError("invalid gateway daily row")
            gateway_days[day] = gateway_days.get(day, 0.0) + cost
            if not math.isfinite(gateway_days[day]):
                raise ValueError("invalid gateway daily sum")
            if gateway_requests is not None:
                try:
                    requests = int(row["request_count"])
                    gateway_requests = gateway_requests + requests if requests >= 0 else None
                except (KeyError, ValueError, TypeError):
                    gateway_requests = None
        gateway_total = sum(gateway_days.values())
        if not math.isfinite(gateway_total):
            raise ValueError("invalid gateway total")
        gateway_state = "available"
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        gateway_days.clear()
        gateway_total = None
        gateway_requests = 0
        status = f" (HTTP {error.code})" if isinstance(error, urllib.error.HTTPError) else ""
        gateway_state = f"Gateway daily billing unavailable{status}"

if detail and gateway_total is not None:
    try:
        path = "/v1/report?" + urllib.parse.urlencode({
            "start_date": start_date, "end_date": end_date,
            "group_by": "model", "api_key_id": "self"})
        response = get_json(path, authenticated=True)
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ValueError("invalid gateway model report")
        for row in response["results"]:
            if not isinstance(row, dict):
                raise ValueError("invalid gateway model row")
            if isinstance(row["total_cost"], bool):
                raise ValueError("invalid gateway model cost")
            cost = float(row["total_cost"])
            requests = int(row.get("request_count") or 0)
            model = row.get("model")
            if (not math.isfinite(cost) or cost < 0 or requests < 0
                    or not isinstance(model, str)):
                raise ValueError("invalid gateway model row")
            bucket = gateway_models.setdefault(model, {
                "cost": 0.0, "requests": 0, "input": 0, "output": 0,
                "cache_read": 0, "cache_write": 0})
            bucket["cost"] += cost
            if not math.isfinite(bucket["cost"]):
                raise ValueError("invalid gateway model sum")
            bucket["requests"] += requests
            for field, report_field in (("input", "input_tokens"),
                                        ("output", "output_tokens"),
                                        ("cache_read", "cached_input_tokens"),
                                        ("cache_write", "cache_creation_input_tokens")):
                bucket[field] += int(row.get(report_field) or 0)
        model_total = sum(row["cost"] for row in gateway_models.values())
        if not math.isfinite(model_total):
            gateway_models.clear()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        gateway_models.clear()

sources = {source: {} for source in ("claude", "codex")}
days = {}
unknown_prices = set()
unknown_days = set()
priced_days = set()


def add(source, model, dt, tokens, cost):
    bucket = sources[source].setdefault(model, {"requests": 0, "input": 0, "output": 0,
                                                "cache_read": 0, "cache_write": 0,
                                                "cost": 0.0, "unpriced_requests": 0})
    bucket["requests"] += 1
    for field, amount in zip(("input", "output", "cache_read", "cache_write"), tokens):
        bucket[field] += amount
    if cost is not None:
        bucket["cost"] += cost
        priced_days.add((dt.date().isoformat(), source))
    else:
        bucket["unpriced_requests"] += 1
        unknown_prices.add(f"{source}:{model}")
        unknown_days.add((dt.date().isoformat(), source))
    day = days.setdefault(dt.date().isoformat(), {"claude": 0.0, "codex": 0.0, "vercel": 0.0})
    if cost is not None:
        day[source] += cost


for gid, message in claude_messages.items():
    model = message["model"]
    if model.startswith("claude-code/"):
        model = model[len("claude-code/"):]
    dt = message["datetime"]
    tokens = message["tokens"]
    if gid.startswith("gen_"):
        continue
    cost = claude_cost(model, tokens)
    add("claude", model, dt, tokens[:4], cost)
    for advisor in message["advisors"]:
        advisor_model = advisor.get("model") or "unknown"
        advisor_tokens = tuple(advisor.get(key, 0) or 0 for key in
                               ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                "cache_creation_input_tokens"))
        advisor_1h = (advisor.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0
        add("claude", advisor_model, dt, advisor_tokens,
            claude_cost(advisor_model, (*advisor_tokens, advisor_1h)))

for model, dt, usage in codex_requests:
    prompt = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cached_input_tokens", 0) or 0
    cache_write = usage.get("cache_write_input_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    try:
        cost = codex_cost(model, usage)
        if cost is not None and not math.isfinite(cost):
            cost = None
    except (TypeError, ValueError, AttributeError, KeyError):
        cost = None
    add("codex", model, dt, (max(prompt - cache_read - cache_write, 0), output,
                              cache_read, cache_write), cost)

for day, cost in gateway_days.items():
    days.setdefault(day, {"claude": 0.0, "codex": 0.0, "vercel": 0.0})["vercel"] += cost

claude_total = sum(item["cost"] for item in sources["claude"].values())
codex_total = sum(item["cost"] for item in sources["codex"].values())
try:
    detected_settings = json.loads((claude_dir / "settings.json").read_text())
    settings_env = detected_settings.get("env", {}) if isinstance(detected_settings, dict) else {}
    settings_base = settings_env.get("ANTHROPIC_BASE_URL", "") if isinstance(settings_env, dict) else ""
except (OSError, ValueError):
    detected_settings = {}
    settings_base = ""
try:
    settings_gateway = urllib.parse.urlparse(settings_base).hostname == "ai-gateway.vercel.sh"
    environment_gateway = (urllib.parse.urlparse(os.environ.get("ANTHROPIC_BASE_URL", "")).hostname
                           == "ai-gateway.vercel.sh")
except (TypeError, ValueError):
    settings_gateway = False
    environment_gateway = False
native_environment = (not environment_gateway and any(os.environ.get(key, "").strip()
                      for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                  "ANTHROPIC_ANALYTICS_API_KEY")))
claude_setup = (projects.exists() or identity_path.exists() or (claude_dir / ".credentials.json").exists()
                or (claude_dir.exists() and not settings_gateway) or native_environment)
codex_setup = codex_dir.exists()

claude_billed_days = {}
claude_billed_total = None
claude_as_of = None
claude_billing_key = ""
claude_billing_state = "personal billing unavailable"
claude_billing_attempted = False


def native_key():
    explicit = os.environ.get("ANTHROPIC_ANALYTICS_API_KEY", "").strip()
    if explicit:
        return explicit
    env_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    env_auth = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    settings_env = detected_settings.get("env", {}) if isinstance(detected_settings, dict) else {}
    if not isinstance(settings_env, dict):
        settings_env = {}
    settings_base = settings_env.get("ANTHROPIC_BASE_URL", "")
    if env_base or env_key or env_auth:
        if env_base and urllib.parse.urlparse(env_base).hostname != "api.anthropic.com":
            return ""
        if not env_base and settings_base and urllib.parse.urlparse(settings_base).hostname != "api.anthropic.com":
            return ""
        return env_key
    if settings_base and urllib.parse.urlparse(settings_base).hostname != "api.anthropic.com":
        return ""
    candidate = settings_env.get("ANTHROPIC_API_KEY")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    helper = detected_settings.get("apiKeyHelper") if isinstance(detected_settings, dict) else None
    if isinstance(helper, str) and helper.strip():
        try:
            result = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    for path in (identity_path, claude_dir / ".credentials.json"):
        try:
            candidate = json.loads(path.read_text()).get("primaryApiKey")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        except (OSError, ValueError, AttributeError):
            pass
    if sys.platform == "darwin":
        account = os.environ.get("USER", "") or "claude-code-user"
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", account):
            account = "claude-code-user"
        config = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        config = config if config is not None else os.environ.get("CLAUDE_CONFIG_DIR")
        suffix = ("-" + hashlib.sha256(unicodedata.normalize("NFC", config).encode()).hexdigest()[:8]
                  if config else "")
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", account, "-w",
                 "-s", "Claude Code" + suffix],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


def personal_report(identity, key):
    own_id = identity["accountUuid"]
    own_email = identity.get("emailAddress", "")
    start = datetime.combine(cutoff_day, datetime.min.time(), timezone.utc)
    stop = datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc)
    opener = urllib.request.build_opener(NoRedirect)
    amounts = {}
    refreshes = []
    actor_seen = False
    cursor_start = start
    while cursor_start < stop:
        cursor_end = min(cursor_start + timedelta(days=31), stop)
        page = None
        seen_pages = set()
        while True:
            query = {
                "starting_at": cursor_start.isoformat().replace("+00:00", "Z"),
                "ending_at": cursor_end.isoformat().replace("+00:00", "Z"),
                "bucket_width": "1d", "products[]": "claude_code",
                "user_ids[]": own_id,
            }
            if page:
                query["page"] = page
            url = ("https://api.anthropic.com/v1/organizations/analytics/user_cost_report?"
                   + urllib.parse.urlencode(query))
            request = urllib.request.Request(
                url, headers={"X-Api-Key": key, "anthropic-version": "2023-06-01"})
            with opener.open(request, timeout=10) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("invalid personal report")
            refreshed = timestamp(payload.get("data_refreshed_at"))
            if refreshed is None or refreshed < start:
                raise ValueError("missing report freshness")
            refreshes.append(refreshed)
            for row in payload["data"]:
                if not isinstance(row, dict) or row.get("currency") != "USD":
                    raise ValueError("invalid personal row")
                actor = row.get("actor")
                if not isinstance(actor, dict) or actor.get("type") != "user_actor":
                    raise ValueError("personal billing identity not verified")
                actor_email = actor.get("email_address")
                if (isinstance(own_email, str) and own_email and
                        isinstance(actor_email, str) and actor_email and
                        actor_email.casefold() != own_email.casefold()):
                    raise ValueError("personal billing identity not verified")
                matched = (actor.get("user_id") == own_id or
                           (isinstance(own_email, str) and own_email and
                            isinstance(actor.get("email_address"), str) and
                            actor["email_address"].casefold() == own_email.casefold()))
                if not matched:
                    raise ValueError("personal billing identity not verified")
                actor_seen = True
                row_start = timestamp(row.get("starting_at"))
                row_end = timestamp(row.get("ending_at"))
                if (row_start is None or row_end is None or row_start.hour or row_start.minute
                        or row_start.second or row_start.microsecond
                        or row_end != row_start + timedelta(days=1)
                        or not cursor_start <= row_start < cursor_end
                        or not isinstance(row.get("amount"), str)):
                    raise ValueError("invalid personal day")
                try:
                    cents = Decimal(row["amount"])
                except InvalidOperation:
                    raise ValueError("invalid personal cost")
                if not cents.is_finite() or cents < 0:
                    raise ValueError("invalid personal cost")
                day = row_start.date().isoformat()
                if day in amounts:
                    raise ValueError("duplicate personal day")
                amounts[day] = cents / 100
            more = payload.get("has_more")
            if not isinstance(more, bool):
                raise ValueError("invalid pagination")
            if not more:
                break
            page = payload.get("next_page")
            if not isinstance(page, str) or not page or page in seen_pages:
                raise ValueError("invalid pagination")
            seen_pages.add(page)
        cursor_start = cursor_end
    if not actor_seen:
        raise ValueError("personal billing identity not verified")
    as_of = min(refreshes)
    if any(day > as_of.date().isoformat() for day in amounts):
        raise ValueError("personal report newer than refresh")
    return amounts, sum(amounts.values(), Decimal(0)), as_of


if claude_setup:
    try:
        identity = json.loads(identity_path.read_text()).get("oauthAccount") if identity_path.exists() else None
        if (not isinstance(identity, dict) or
                not isinstance(identity.get("accountUuid"), str) or
                not identity["accountUuid"]):
            claude_billing_state = "own identity unavailable"
        else:
            key = native_key()
            if not key or (token and key == token):
                claude_billing_state = "personal billing API key unavailable"
            else:
                claude_billing_key = key
                claude_billing_attempted = True
                claude_billed_days, claude_billed_total, claude_as_of = personal_report(identity, key)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as error:
        claude_billed_days.clear()
        claude_billed_total = None
        if isinstance(error, urllib.error.HTTPError):
            claude_billing_state = f"personal billing unavailable (HTTP {error.code})"
        elif "identity" in str(error):
            claude_billing_state = "personal billing identity not verified"
        else:
            claude_billing_state = "personal billing unavailable"

openai_days = {}
openai_total = None
openai_state = "personal billing unavailable"
openai_billing_attempted = False
openai_setup = codex_setup or any(os.environ.get(name, "").strip() for name in
                                  ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_API_KEY_ID"))


def openai_key():
    explicit = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if explicit:
        return explicit
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
        base = os.environ.get(name, "")
        if base and urllib.parse.urlparse(base).hostname != "api.openai.com":
            return ""
    candidate = os.environ.get("OPENAI_API_KEY", "").strip()
    if candidate:
        return candidate
    try:
        auth = json.loads((codex_dir / "auth.json").read_text())
        candidate = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
        return candidate.strip() if isinstance(candidate, str) else ""
    except (OSError, ValueError):
        return ""


def openai_report(key, own_id):
    start = datetime.combine(cutoff_day, datetime.min.time(), timezone.utc)
    stop = datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc)
    query = {
        "start_time": int(start.timestamp()), "end_time": int(stop.timestamp()),
        "bucket_width": "1d", "limit": 180, "api_key_ids[]": own_id,
        "group_by[]": "api_key_id",
    }
    opener = urllib.request.build_opener(NoRedirect)
    amounts = {}
    seen_pages = set()
    while True:
        url = "https://api.openai.com/v1/organization/costs?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with opener.open(request, timeout=10) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get("object") != "page" or not isinstance(payload.get("data"), list):
            raise ValueError("invalid OpenAI report")
        for bucket in payload["data"]:
            if not isinstance(bucket, dict) or bucket.get("object") != "bucket":
                raise ValueError("invalid OpenAI bucket")
            start_time, end_time = bucket.get("start_time"), bucket.get("end_time")
            if (isinstance(start_time, bool) or not isinstance(start_time, int) or
                    isinstance(end_time, bool) or not isinstance(end_time, int) or
                    start_time % 86400 or end_time != start_time + 86400 or
                    not int(start.timestamp()) <= start_time < int(stop.timestamp()) or
                    not isinstance(bucket.get("results"), list)):
                raise ValueError("invalid OpenAI day")
            day = datetime.fromtimestamp(start_time, timezone.utc).date().isoformat()
            if day in amounts:
                raise ValueError("duplicate OpenAI day")
            amount = Decimal(0)
            seen_key = False
            for result in bucket["results"]:
                if (not isinstance(result, dict) or
                        result.get("object") != "organization.costs.result" or
                        result.get("api_key_id") != own_id or seen_key):
                    raise ValueError("OpenAI key identity mismatch")
                seen_key = True
                value = result.get("amount")
                if not isinstance(value, dict) or value.get("currency") != "usd":
                    raise ValueError("invalid OpenAI amount")
                raw = value.get("value")
                if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                    raise ValueError("invalid OpenAI amount")
                try:
                    cost = Decimal(str(raw))
                except InvalidOperation:
                    raise ValueError("invalid OpenAI amount")
                if not cost.is_finite() or cost < 0:
                    raise ValueError("invalid OpenAI amount")
                amount += cost
            amounts[day] = amount
        more = payload.get("has_more")
        if not isinstance(more, bool):
            raise ValueError("invalid OpenAI pagination")
        if not more:
            break
        page = payload.get("next_page")
        if not isinstance(page, str) or not page or page in seen_pages:
            raise ValueError("invalid OpenAI pagination")
        seen_pages.add(page)
        query["page"] = page
    return amounts, sum(amounts.values(), Decimal(0))


if openai_setup:
    own_id = os.environ.get("OPENAI_API_KEY_ID", "").strip()
    if not re.fullmatch(r"(?:key_|key-)[A-Za-z0-9_-]+", own_id):
        openai_state = "personal API-key ID unavailable"
    else:
        try:
            key = openai_key()
            if (not key or (token and key == token) or
                    (claude_billing_key and key == claude_billing_key) or
                    key == os.environ.get("ANTHROPIC_API_KEY", "") or
                    key == os.environ.get("ANTHROPIC_ANALYTICS_API_KEY", "")):
                openai_state = "personal billing API key unavailable"
            else:
                openai_billing_attempted = True
                openai_days, openai_total = openai_report(key, own_id)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as error:
            openai_days.clear()
            openai_total = None
            status = f" (HTTP {error.code})" if isinstance(error, urllib.error.HTTPError) else ""
            openai_state = f"personal billing unavailable{status}"


billing_incomplete = ((token and gateway_total is None) or
                      (claude_billing_attempted and claude_billed_total is None) or
                      (openai_billing_attempted and openai_total is None))


def source_money(source, amount):
    label = money(amount, source != "vercel")
    return label + " + unpriced" if any(name.startswith(source + ":") for name in unknown_prices) else label


def display_name(model):
    return canonical_model(model).split("/")[-1]


def fit(label, width):
    return label if len(label) <= width else label[:width - 1] + "…"


USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
COLORS = {"vercel": (57, 135, 229), "claude": (217, 89, 38), "codex": (25, 158, 112)}
STYLES = {"title": "1", "muted": "2", "warning": "1;38;2;214;153;43"}
CHARS = {"vercel": "█", "claude": "▓", "codex": "░"}


def paint(value, source):
    if not USE_COLOR or not value:
        return value
    if source in STYLES:
        return f"\x1b[{STYLES[source]}m{value}\x1b[0m"
    r, g, b = COLORS[source]
    return f"\x1b[38;2;{r};{g};{b}m{value}\x1b[0m"


def cells(value, total, width):
    return max(1, round(value / total * width)) if value >= 0.01 and total > 0 else 0


def dashboard():
    print()
    title = "AI usage · "
    updated = now.strftime("updated %H:%M %Z")
    gap = " " * max(1, WIDTH - len(title) - len(window_label) - len(updated))
    print(paint(title, "title") + paint(window_label, "muted") + gap + paint(updated, "muted"))
    print()
    available = [("VERCEL", gateway_total), ("CLAUDE", claude_billed_total),
                 ("OPENAI", openai_total)]
    available = [(name, amount) for name, amount in available if amount is not None]
    if not available:
        total_label = "—"
        total_source = "TOTAL · VERCEL UNAVAILABLE" if token else "TOTAL · BILLED"
    else:
        reported_total = sum((Decimal(str(amount)) for _, amount in available), Decimal(0))
        total_label = money(reported_total)
        if len(available) == 1:
            source_name = available[0][0]
            total_source = ("TOTAL · VERCEL BILLED" if source_name == "VERCEL"
                            else f"TOTAL · {source_name} REPORTED")
        elif billing_incomplete:
            total_source = "TOTAL · AVAILABLE BILLED"
        elif claude_as_of is not None and claude_as_of.date() < today:
            total_source = "TOTAL · REPORTED SUBTOTAL"
        else:
            total_source = "TOTAL · REPORTED COST"
    total_line = f"{total_source:<89}{total_label:>10}"
    print(paint(total_line, "warning" if not available and token else "title"))

    if openai_total is not None:
        print()
        if gateway_total is not None:
            print(f"{paint(CHARS['vercel'], 'vercel')} {'VERCEL · this key · billed':<87}"
                  f"{money(gateway_total):>10}")
        if claude_billed_total is not None:
            as_of = claude_as_of.strftime("%Y-%m-%d %H:%M UTC")
            claude_label = f"CLAUDE · this user · reported cost · as of {as_of}"
            print(f"{paint(CHARS['claude'], 'claude')} {claude_label:<87}"
                  f"{money(claude_billed_total):>10}")
        print(f"{paint(CHARS['codex'], 'codex')} {'OPENAI · this key · reported cost':<87}"
              f"{money(openai_total):>10}")
        print()
        present = [(name, source_days) for available, name, source_days in
                   ((gateway_total is not None, "VERCEL", gateway_days),
                    (claude_billed_total is not None, "CLAUDE", claude_billed_days),
                    (True, "OPENAI", openai_days)) if available]
        headings = "".join(f"{name:>11}" for name, _ in present)
        spacer = max(1, 99 - 2 - 11 - len(headings) - 10)
        print(f"  {'BY UTC DAY':<11}{headings}{'':{spacer}}{'TOTAL':>10}")
        for offset in range((today - cutoff_day).days, -1, -1):
            day = (cutoff_day + timedelta(days=offset)).isoformat()
            pending = claude_billed_total is not None and day > claude_as_of.date().isoformat()
            amounts = [source_days.get(day, 0) for _, source_days in present]
            cells_text = "".join(f"{'pending' if name == 'CLAUDE' and pending else money(amount):>11}"
                                 for (name, _), amount in zip(present, amounts))
            daily_total = "—" if pending else money(sum((Decimal(str(amount)) for amount in amounts),
                                                        Decimal(0)))
            print(f"  {day:<11}{cells_text}{'':{spacer}}{daily_total:>10}")
    elif claude_billed_total is not None:
        print()
        if gateway_total is not None:
            print(f"{paint(CHARS['vercel'], 'vercel')} {'VERCEL · this key · billed':<87}"
                  f"{money(gateway_total):>10}")
        as_of = claude_as_of.strftime("%Y-%m-%d %H:%M UTC")
        claude_label = f"CLAUDE · this user · reported cost · as of {as_of}"
        print(f"{paint(CHARS['claude'], 'claude')} {claude_label:<87}"
              f"{money(claude_billed_total):>10}")
        print()
        if gateway_total is not None:
            print(f"  {'BY UTC DAY':<11}{'VERCEL':>11}{'CLAUDE':>11}{'':54}{'TOTAL':>10}")
        else:
            print(f"  {'BY UTC DAY':<11}{'CLAUDE':>11}{'':65}{'TOTAL':>10}")
        for offset in range((today - cutoff_day).days, -1, -1):
            day = (cutoff_day + timedelta(days=offset)).isoformat()
            pending = day > claude_as_of.date().isoformat()
            claude_amount = claude_billed_days.get(day, Decimal(0))
            claude_cell = "pending" if pending else money(claude_amount)
            daily_total = "—" if pending else money(claude_amount + Decimal(
                str(gateway_days.get(day, 0.0))))
            if gateway_total is not None:
                vercel_cell = money(gateway_days.get(day, 0.0))
                print(f"  {day:<11}{vercel_cell:>11}{claude_cell:>11}{'':54}{daily_total:>10}")
            else:
                print(f"  {day:<11}{claude_cell:>11}{'':65}{daily_total:>10}")
    elif gateway_total is not None:
        print()
        if gateway_total >= 0.01:
            print("  " + paint(CHARS["vercel"] * 97, "vercel"))
        print()
        print(f"{paint(CHARS['vercel'], 'vercel')} {'VERCEL · this key · billed':<87}"
              f"{money(gateway_total):>10}")
        if gateway_total == 0:
            print("  no billed usage in this window")

        print()
        print(f"  {'BY UTC DAY':<11}{'VERCEL':>11} {'':63} {'TOTAL':>10}")
        largest_day = max(gateway_days.values(), default=0)
        day_count = (today - cutoff_day).days
        for offset in range(day_count, -1, -1):
            day = (cutoff_day + timedelta(days=offset)).isoformat()
            amount = gateway_days.get(day, 0.0)
            bar_count = min(63, cells(amount, largest_day, 63))
            bar = paint(CHARS["vercel"] * bar_count, "vercel")
            daily_amount = money(amount)
            print(f"  {day:<11}{daily_amount:>11} {bar}{' ' * (63 - bar_count)} "
                  f"{daily_amount:>10}")



def detail_view():
    detail_width = WIDTH

    def visible_width(value):
        plain = re.sub(r"\x1b\[[0-9;]*m", "", value)
        return sum(0 if unicodedata.combining(char) else
                   2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
                   for char in plain)

    def output_line(value="", source=None):
        if not value:
            print()
            return
        padding = detail_width - visible_width(value)
        if padding < 0:
            raise ValueError("detail line exceeds display width")
        print(paint(value + " " * padding, source) if source else value + " " * padding)

    def wrapped(value, limit=detail_width):
        remaining = value.strip()
        while remaining:
            if visible_width(remaining) <= limit:
                yield remaining
                break
            used = 0
            cut = 0
            for index, char in enumerate(remaining):
                used += visible_width(char)
                if used > limit:
                    break
                cut = index + 1
            cut = max(cut, 1)
            space = remaining.rfind(" ", 0, cut + 1)
            if space > 0:
                yield remaining[:space].rstrip()
                remaining = remaining[space + 1:].lstrip()
            else:
                yield remaining[:cut]
                remaining = remaining[cut:].lstrip()

    def emit(value="", source=None):
        if not value:
            output_line()
        else:
            for piece in wrapped(value):
                output_line(piece, source)

    def daily_header(columns):
        total_width = 16 if len(columns) > 1 else 0
        column_width = (detail_width - 12 - total_width) // len(columns)
        spare = detail_width - 12 - total_width - column_width * len(columns)
        heading = f"{'  DATE':<12}"
        heading += "".join(paint(f"{label:>{column_width}}", source)
                           for source, label, _ in columns)
        if total_width:
            heading += " " * spare + f"{'TOTAL':>{total_width}}"
        output_line(heading)
        provenance = " " * 12
        provenance += "".join(paint(f"{origin:>{column_width}}", source)
                              for source, _, origin in columns)
        if total_width:
            provenance += " " * spare + f"{'combined':>{total_width}}"
        output_line(provenance)

    def daily_row(day, columns, total=None):
        total_width = 16 if total is not None else 0
        column_width = (detail_width - 12 - total_width) // len(columns)
        spare = detail_width - 12 - total_width - column_width * len(columns)
        row = f"  {day:<10}"
        overflow = []
        for source, label, value in columns:
            if visible_width(value) > column_width:
                overflow.append((label, value, source))
                value = "↳"
            row += paint(f"{value:>{column_width}}", source)
        row += " " * spare
        if total is not None:
            if visible_width(total) > total_width:
                overflow.append(("TOTAL", total, None))
                total = "↳"
            row += f"{total:>{total_width}}"
        output_line(row)
        for label, value, source in overflow:
            emit(f"{label}: {value}", source)

    output_line()
    output_line(paint("AI usage detail · ", "title") + paint(window_label, "muted"))
    emit(f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}", "muted")
    output_line("=" * detail_width)
    reported_total = ((claude_billed_total or Decimal(0)) + (openai_total or Decimal(0)) +
                      Decimal(str(gateway_total or 0))
                      if claude_billed_total is not None or openai_total is not None else gateway_total)
    total_heading = ("REPORTED TOTAL" if claude_billed_total is not None or openai_total is not None
                     else "BILLED TOTAL")
    if claude_as_of is not None and claude_as_of.date() < today:
        total_heading = "REPORTED SUBTOTAL"
    included = [label for available, label in ((gateway_total is not None, "Vercel"),
                                               (claude_billed_total is not None, "Claude"),
                                               (openai_total is not None, "OpenAI")) if available]
    if billing_incomplete and included != ["Vercel"] and reported_total is not None:
        total_heading = "AVAILABLE BILLED"
    if reported_total is None:
        emit(f"{total_heading}: unavailable", "warning" if token else "title")
    else:
        providers = " + ".join(included) + (" only" if len(included) == 1 else "")
        emit(f"{total_heading}: {money(reported_total)} ({providers})", "title")
    if token and gateway_total is None:
        status = gateway_state[len("Gateway daily billing unavailable"):]
        emit("Billing unavailable: Vercel daily report" + status, "warning")
    local_sources = [source for source in ("claude", "codex") if sources[source]]
    fallback_sources = [source.title() for source in local_sources
                        if (claude_billed_total if source == "claude" else openai_total) is None
                        and any(name == source for _, name in priced_days)]
    if local_sources:
        local_label = (money(claude_total + codex_total, True) if priced_days
                       else "unavailable (no priced records)")
        emit(f"LOCAL ESTIMATE: {local_label}")
        if fallback_sources:
            emit(f"* {' and '.join(fallback_sources)} estimated from local tokens.", "muted")
    if included:
        output_line()
        emit("PROVIDER BILLING")
    if gateway_total is not None:
        bill = f"VERCEL · this key · billed: {money(gateway_total)}"
        request_label = f" ({gateway_requests:,} req)" if gateway_requests is not None else ""
        if visible_width(bill + request_label) <= detail_width:
            emit(bill + request_label, "vercel")
        else:
            emit(bill, "vercel")
            emit(f"{gateway_requests:,} requests", "vercel")
    if claude_billed_total is not None:
        emit(f"CLAUDE · reported: {money(claude_billed_total)}", "claude")
        emit(f"This user · as of {claude_as_of.strftime('%Y-%m-%d %H:%M UTC')}", "claude")
    if openai_total is not None:
        emit(f"OPENAI · this key · reported: {money(openai_total)}", "codex")
    if local_sources:
        output_line()
        emit("LOCAL ESTIMATES · saved history")
        for source in local_sources:
            amount = claude_total if source == "claude" else codex_total
            label = (source_money(source, amount) if any(name == source for _, name in priced_days)
                     else "unavailable (unpriced records)")
            emit(f"{source.upper()} · local estimate: {label}", source)
            unpriced = sum(info["unpriced_requests"] for info in sources[source].values())
            if unpriced:
                records = "record" if unpriced == 1 else "records"
                note = f"Unpriced local usage: {source.title()} · {unpriced} {records}"
                recorded_tokens = (sum(usage.get("total_tokens", 0) or 0
                                       for model, _, usage in codex_requests if model == "unknown")
                                   if source == "codex" else 0)
                if recorded_tokens:
                    note += f" · {recorded_tokens:,} reported tokens"
                if f"{source}:unknown" in unknown_prices:
                    note += " · missing model"
                elif catalog_unavailable:
                    note += " · catalog unavailable"
                else:
                    note += " · price unavailable"
                emit(note, "warning")
    daily_sources = []
    if gateway_total is not None:
        daily_sources.append(("vercel", "VERCEL", "billed"))
    if claude_billed_total is not None:
        daily_sources.append(("claude", "CLAUDE", "reported"))
    elif sources["claude"]:
        daily_sources.append(("claude", "CLAUDE", "local"))
    if openai_total is not None:
        daily_sources.append(("codex", "CODEX", "reported"))
    elif sources["codex"]:
        daily_sources.append(("codex", "CODEX", "local"))
    if daily_sources:
        output_line()
        has_local_daily = any(origin == "local" for _, _, origin in daily_sources)
        emit("BY UTC DAY · BILLING AND ESTIMATES" if has_local_daily else "BY UTC DAY · BILLING")
        daily_header(daily_sources)
        no_saved_day = False
        for offset in range((today - cutoff_day).days, -1, -1):
            day = (cutoff_day + timedelta(days=offset)).isoformat()
            columns = []
            day_total = Decimal(0)
            has_numeric = estimated = excluded = pending = unpriced = False
            for source, label, origin in daily_sources:
                if origin == "billed":
                    amount = Decimal(str(gateway_days.get(day, 0)))
                    value = money(amount)
                    day_total += amount
                    has_numeric = True
                elif origin == "reported":
                    if source == "claude" and day > claude_as_of.date().isoformat():
                        value = "pending"
                        pending = excluded = True
                    else:
                        source_days = claude_billed_days if source == "claude" else openai_days
                        amount = source_days.get(day, Decimal(0))
                        value = money(amount)
                        day_total += amount
                        has_numeric = True
                elif (day, source) in priced_days:
                    amount = Decimal(str(days[day][source]))
                    value = money(amount, True)
                    day_total += amount
                    has_numeric = estimated = True
                    if (day, source) in unknown_days:
                        value += "*"
                        excluded = True
                else:
                    value = "unpriced" if (day, source) in unknown_days else "—"
                    if value == "unpriced":
                        unpriced = excluded = True
                    else:
                        no_saved_day = True
                columns.append((source, label, value))
            combined = (money(day_total, estimated) + ("*" if excluded else "") if has_numeric
                        else "pending" if pending else "unpriced" if unpriced else "—")
            daily_row(day, columns, combined if len(daily_sources) > 1 else None)
        output_line("-" * detail_width)
        columns = []
        selected_total = Decimal(0)
        has_numeric = estimated = excluded = pending = unpriced = False
        for source, label, origin in daily_sources:
            source_excluded = False
            if origin == "billed":
                amount = Decimal(str(gateway_total))
            elif origin == "reported":
                amount = claude_billed_total if source == "claude" else openai_total
                if source == "claude" and claude_as_of.date() < today:
                    source_excluded = pending = True
            else:
                source_excluded = any(name == source for _, name in unknown_days)
                if any(name == source for _, name in priced_days):
                    amount = Decimal(str(claude_total if source == "claude" else codex_total))
                    estimated = True
                else:
                    amount = None
                    unpriced = True
            if amount is None:
                value = "unpriced"
            else:
                value = money(amount, origin == "local") + ("*" if source_excluded else "")
                selected_total += amount
                has_numeric = True
            excluded |= source_excluded
            columns.append((source, label, value))
        combined = (money(selected_total, estimated) + ("*" if excluded else "") if has_numeric
                    else "pending" if pending else "unpriced" if unpriced else "—")
        daily_row("TOTAL", columns, combined if len(daily_sources) > 1 else None)
        if no_saved_day:
            emit("— No saved usage on this day.", "muted")
        if excluded:
            emit("* Excludes unpriced or pending costs.", "warning")
    if not (gateway_models or local_sources or gateway_total):
        return
    output_line()
    emit("BY MODEL · provider costs and local estimates" if local_sources else
         "BY MODEL · provider costs")
    output_line()
    name_width = detail_width - 53
    output_line(f"{'Source / model':<{name_width}}{'Reqs':>7}{'Input':>8}"
                f"{'Output':>8}{'CacheR':>8}{'CacheW':>8}{'Cost':>14}")
    output_line("-" * detail_width)
    if (gateway_total is not None and not gateway_models and
            (gateway_total > 0 or (gateway_requests or 0) > 0)):
        emit("Vercel model breakdown unavailable; daily billing retained.", "vercel")
    hidden_vercel = 0
    for source in ("vercel", "claude", "codex"):
        rows = gateway_models if source == "vercel" else sources[source]
        for model, info in sorted(rows.items(), key=lambda item: -item[1]["cost"]):
            if source == "vercel":
                if info["cost"] == 0 and all(info[field] == 0 for field in
                                             ("input", "output", "cache_read", "cache_write")):
                    hidden_vercel += 1
                    continue
                tokens = info
                note = money(info["cost"])
            else:
                tokens = info
                note = ("unpriced" if f"{source}:{model}" in unknown_prices
                        else money(info["cost"], True))
            shown_model = display_name(model)
            model_label = f"{source.upper()} {shown_model}"
            values = (f"{info['requests']:,}", short(tokens.get("input", 0)),
                      short(tokens.get("output", 0)), short(tokens.get("cache_read", 0)),
                      short(tokens.get("cache_write", 0)))
            if visible_width(model_label) > name_width:
                if visible_width(model_label) <= detail_width:
                    output_line(model_label, source)
                elif visible_width(shown_model) <= detail_width:
                    output_line(source.upper(), source)
                    output_line(shown_model, source)
                else:
                    emit(model_label, source)
            name = model_label if visible_width(model_label) <= name_width else ""
            row = name + " " * (name_width - visible_width(name))
            overflow = []
            for heading, value, width in zip(("Reqs", "Input", "Output", "CacheR", "CacheW",
                                              "Cost"), (*values, note), (7, 8, 8, 8, 8, 14)):
                if visible_width(value) > width:
                    overflow.append((heading, value))
                    value = "↳"
                row += f"{value:>{width}}"
            output_line(row, source)
            for heading, value in overflow:
                emit(f"{heading}: {value}", source)
    if hidden_vercel:
        emit(f"Vercel: {hidden_vercel} empty model rows hidden.", "vercel")
    output_line("-" * detail_width)


detail_view() if detail else dashboard()
PYTHON
