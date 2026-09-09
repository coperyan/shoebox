# TCDB automation

Browser automation for the [Trading Card Database](https://www.tcdb.com)
(TCDB): the site where the collection is catalogued. Today it covers one
workflow, **advanced search by card number**; the module is laid out so
further TCDB actions slot in beside it.

## Why a real browser

tcdb.com is behind Cloudflare and has no public API. Plain HTTP gets the
"Just a moment…" interstitial, so `shoebox` drives a visible Chrome through
**undetected-chromedriver** (already used for topps.com and 130point). Chrome
runs with a **persistent profile** (`tcdb.profile_dir`, default
`data/tcdb_chrome_profile`), so two things survive between runs:

- the Cloudflare clearance cookie, and
- your TCDB login, if you tick *Remember* when you sign in.

Credentials never pass through shoebox. When a login is needed the tool opens
`Login.cfm` in the Chrome window, asks you to sign in there, and polls the page
header until TCDB shows you as signed in.

## `shoebox tcdb-search`

```
shoebox tcdb-search [card numbers ...] [--name Bonds] [--year 1993] [--set-name "Upper Deck"]
                    [--set-type M] [--category Baseball] [--team Giants] [--note ...]
                    [--no-login] [--rows N] [--profile-dir PATH]
```

1. Starts Chrome on the TCDB profile.
2. Unless `--no-login`, makes sure you are logged in (prompting once per session
   if the profile has no valid login).
3. Searches any card numbers given on the command line.
4. Drops into a prompt. Each line is a card number searched with the current
   defaults; matches print as a table (number, card, note, URL) and the same
   results page is left open in Chrome so you can click the card and add it to
   your collection.

Session commands:

| Input | Effect |
|---|---|
| `25` | Search card #25 with the current defaults |
| `set year=1993 set_name="Upper Deck"` | Change one or more defaults (quote values with spaces) |
| `set year=` | Clear a default |
| `show` | Print the current defaults |
| `open 3` | Open result #3 from the last search in Chrome |
| `help` | List commands and fields |
| `quit` / `q` / `exit` / Ctrl-D | End the session (closes Chrome) |

Fields (matching the form on AdvancedSearch.cfm): `category`, `year`,
`set_name`, `set_type`, `card_number`, `name`, `team`, `note`. `category` is
one of TCDB's sport buckets (`Baseball`, `Basketball`, … case-insensitive);
`set_type` takes the form code or its label (`M` or `Minor League`; blank =
Any).

### Defaults

Precedence, highest first: `set …` in the session → CLI flags →
`tcdb.search_defaults` in `configs/app.yaml` → built-ins (`category: Baseball`,
everything else blank).

```yaml
tcdb:
  profile_dir: data/tcdb_chrome_profile
  login_timeout_s: 300
  search_defaults:
    category: Baseball
    name: Bonds
```

### Example

```
$ shoebox tcdb-search --name Bonds
Please log in to TCDB in the Chrome window that just opened (waiting up to 300s; tick 'Remember' to skip this next time).
Defaults: category='Baseball', name='Bonds'
card #> 25
      TCDB: category='Baseball', card_number='25', name='Bonds' — 97 result(s)
  #   Card                                             Note              URL
  1   1994 Upper Deck Fun Pack #25 Barry Bonds                           https://www.tcdb.com/ViewCard.cfm/sid/10182/cid/440525/…
  …
card #> set year=1994
Defaults: category='Baseball', year='1994', name='Bonds'
card #> 25
card #> open 1
card #> q
```

## Code layout

| Path | Role |
|---|---|
| `shoebox/models/tcdb.py` | `AdvancedSearchQuery` (one attribute per form field, validation, `to_url()`), `TcdbSearchResult`, `TcdbSearchPage` |
| `shoebox/clients/tcdb/search.py` | Pure HTML parsing: `parse_results`, `is_logged_in`, `is_challenge_page`. Unit-tested against `tests/fixtures/tcdb_view_results.html` |
| `shoebox/clients/tcdb/browser.py` | `TcdbBrowser`: Chrome lifecycle, Cloudflare wait, `ensure_logged_in`, one method per site action (`advanced_search`, `open`) |
| `shoebox/pipelines/tcdb_search.py` | The interactive session; `parse_command` is pure and tested |
| `shoebox/settings.py` → `TcdbSettings` | `profile_dir`, `login_timeout_s`, `search_defaults` |

### Adding another TCDB action

1. Find the page's URL pattern and result markup (the advanced search is a
   plain `GET ViewResults.cfm?MODE=ADVANCED&…` despite the form saying POST).
2. Add any new model to `models/tcdb.py` and a pure parser next to
   `parse_results` in `clients/tcdb/search.py` (or a sibling module such as
   `collection.py`). Save a trimmed real page under `tests/fixtures/` and test
   the parser against it.
3. Add a one-method wrapper on `TcdbBrowser` that calls `self.get(url)` and
   hands the HTML to the parser. `get()` already waits out Cloudflare.
4. Expose it: a new `Command` kind in `pipelines/tcdb_search.py` if it belongs
   in the session (e.g. `add 3` to add result #3 to the collection), or a new
   pipeline plus CLI subcommand if it stands alone.

## Troubleshooting

- **Stuck on "Performing security verification"** — Cloudflare wants a click
  it will not give a bot. Click the checkbox in the Chrome window; the tool
  waits up to 90 s. The clearance is then cached in the profile.
- **"No TCDB login detected within 300s"** — you did not finish logging in.
  Re-run; raise `tcdb.login_timeout_s` if you need longer.
- **Chrome version errors** — undetected-chromedriver reads the installed
  Chrome's major version via `chrome_version`; update Chrome or the
  `undetected-chromedriver` package if the two drift apart.
- **Profile locked** — a previous Chrome on the same profile is still running.
  Quit it (or delete `data/tcdb_chrome_profile/SingletonLock`).
