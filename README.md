# AI Usage

A Bash and Python 3.8+ tool for personal provider charges and whole-team Vercel AI Gateway spend. It uses Python's standard library only.

## Run with curl

```bash
curl -fsSL https://raw.githubusercontent.com/pioneerworks/ai-usage/main/usage.sh | bash -s -- 30d
curl -fsSL https://raw.githubusercontent.com/pioneerworks/ai-usage/main/usage.sh | bash -s -- detail 30d
curl -fsSL https://raw.githubusercontent.com/pioneerworks/ai-usage/main/usage.sh | bash -s -- users 30d
```

## Download and run locally

```bash
curl -fsSL https://raw.githubusercontent.com/pioneerworks/ai-usage/main/usage.sh -o usage.sh
chmod +x usage.sh
./usage.sh 30d
```

## Commands

| Command | Report |
| --- | --- |
| `./usage.sh [today\|Nd]` | Personal provider-reported charges; default `7d` |
| `./usage.sh detail [today\|Nd]` | Personal charges plus local session estimates |
| `./usage.sh users [today\|Nd] [options]` | Whole-team Gateway spend |
| `./usage.sh --help` | Show all commands, options, and examples |

Report commands default to `7d`. Periods use inclusive UTC dates, so `1d` includes yesterday and today so far.

## Options for `users`

| Option | Effect |
| --- | --- |
| `--find TEXT` | Match labels, names, emails, or key names. Keeps global rank and full-report totals. |
| `--keys` | Show raw key names. Use only with the default `api_key_name` grouping; names are not secret tokens. |
| `--top N\|all` | Show the first positive `N` rows or all rows; default is all. |
| `--by day\|model\|provider\|user\|api_key_name` | Group by the selected Gateway dimension. `user` means Gateway user IDs; `api_key_name` is the inferred-person default. |
| `-h`, `--help` | Print help and examples. |

Grouping examples:

```bash
./usage.sh users 30d --by day           # Daily totals, chronological
./usage.sh users 30d --by model         # Spend by model
./usage.sh users 30d --by provider      # Spend by provider
./usage.sh users 30d --by user          # Gateway user IDs
./usage.sh users 30d --by api_key_name  # Inferred people (default)
```

## Reading the reports

Personal totals use available Gateway self, Claude user, and OpenAI API key charges. `detail` adds Claude Code and Codex estimates from saved local tokens and current model prices; these stay separate from billed totals. Missing providers are omitted. Team charges are separate.

`users` ranks whole-team Gateway spend by inferred person. One identity column shows the inferred name, then email, then the original key label for unknown or shared keys. Rows include requests and reported input/output tokens when available. Counts use K/M/B units; missing token counts are omitted or shown as `—`, never estimated. `SHARE` is percent of whole-team spend; each bar scales to the highest spender. A Gateway credential must have access to the team report.

Set `AI_GATEWAY_API_KEY` or use a matching Gateway credential from the environment or Claude settings. On macOS, `AI_GATEWAY_KEYCHAIN_SERVICE` selects the Keychain service; the default is `pioneerworks-ai-gateway`. Credentials are not printed or sent to unrelated hosts.

`detail` reads local Claude Code and Codex session history and caches parsed usage metadata in the user's state directory (`XDG_STATE_HOME` when set). It does not upload transcript text or cache credentials.

`NO_COLOR=1` disables terminal colors.

Run fixture tests with `python3 -B -m unittest discover -s test -p '*_test.py'`.
