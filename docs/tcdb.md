# TCDB automation

Browser automation for the [Trading Card Database](https://www.tcdb.com)
(TCDB): the site where the collection is catalogued. It covers two
workflows, **advanced search by card number** (`tcdb-search`) and **adding
cards to your collection** (`tcdb-add`, or `add N` inside a search session);
the module is laid out so further TCDB actions slot in beside it.

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
   results page is left open in Chrome. `add N` adds result N to your
   collection.

Session commands:

| Input | Effect |
|---|---|
| `25` | Search card #25 with the current defaults |
| `set year=1993 set_name="Upper Deck"` | Change one or more defaults (quote values with spaces) |
| `set year=` | Clear a default |
| `show` | Print the current defaults |
| `open 3` | Open result #3 from the last search in Chrome |
| `add 3` | Add result #3 from the last search to your collection (skipped if you already have it) |
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
card #> add 1
added: 1994 Upper Deck Fun Pack #25 Barry Bonds
card #> q
```

## `shoebox tcdb-add`

```
shoebox tcdb-add ["<year> <set> #<number> <name>" | <ViewCard.cfm link> ...] [--file cards.txt]
                 [--category Baseball] [--dry-run] [--allow-duplicates] [--profile-dir PATH]
```

Adds cards to your collection from their titles, written exactly the way TCDB
titles them (copy the heading of the card page):

```
$ shoebox tcdb-add "2009 Bowman Chrome - X-Fractors #171 Matt Cain" "1993 Upper Deck #25 Barry Bonds"
```

or one per line in a file (blank lines and lines starting with `#` are skipped):

```
# cards.txt
2009 Bowman Chrome - X-Fractors #171 Matt Cain
1993 Upper Deck #25 Barry Bonds
https://www.tcdb.com/ViewCard.cfm/sid/24459/cid/2840262/2009-Bowman-Chrome-X-Fractors-171-Matt-Cain
```

```
$ shoebox tcdb-add --file cards.txt --dry-run   # find everything first, add nothing
$ shoebox tcdb-add --file cards.txt
```

For each line:

1. Every line is checked before Chrome opens. A line that is neither a
   `<year> <set> #<number> <name>` title nor a ViewCard link stops the run.
2. **Title** → advanced search on *year + card number + name* (the first name
   for a multi-player `A / B` card). The set name is left out of the search on
   purpose: TCDB treats a leading `-` as "exclude", and set names are full of
   ` - `. The search result whose title matches yours **exactly** is used.
   Case, accents, curly apostrophes and extra spaces don't matter. Parallels
   are told apart by the full title: `Bowman Chrome #171` and
   `Bowman Chrome - X-Fractors #171` are different cards. No exact match, or
   more than one row with the same title, is reported with what TCDB did find
   and **skipped, never guessed**. Pass the card's ViewCard link to pick one.
   **ViewCard link** → used as is.
3. The card page is opened and its collection box (`#colDiv`, which TCDB
   loads after the page) is read:
   - **Quick Add** button → clicked. shoebox watches the
     `CollectionAdd*_ajax.cfm` request the page makes, then checks that the
     box re-renders showing the card.
   - Card already in the collection (the box shows *add another* / *remove*)
     → skipped as `already_owned`. `--allow-duplicates` clicks *add another*
     instead.
   - Anything else → `no_add_button` / `failed`. The box's HTML is saved to
     `<paths.data_dir>/tcdb_debug/collection_box_<card id>.html` so the
     parser can be fixed.

The run ends with a table (card, result, detail). The exit code is 0 when
every card was `added`, `already_owned`, or `found` (dry run), and 1 otherwise.

Cards go into the collection the card page opens on (your first collection).

## Code layout

| Path | Role |
|---|---|
| `shoebox/models/tcdb.py` | `AdvancedSearchQuery` (one attribute per form field, validation, `to_url()`), `TcdbSearchResult`, `TcdbSearchPage`, `CardSpec`, `TcdbCardPage`, `CollectionWidget`, `CollectionAddResult` |
| `shoebox/clients/tcdb/search.py` | Pure HTML parsing: `parse_results`, `is_logged_in`, `is_challenge_page`. Unit-tested against `tests/fixtures/tcdb_view_results.html` |
| `shoebox/clients/tcdb/card.py` | Pure helpers for adding: `parse_card_spec`, `match_card`, `parse_card_page`, `parse_collection_widget`. Unit-tested against `tests/fixtures/tcdb_view_card.html` |
| `shoebox/clients/tcdb/browser.py` | `TcdbBrowser`: Chrome lifecycle, Cloudflare wait, `ensure_logged_in`, one method per site action (`advanced_search`, `open`, `add_to_collection`) |
| `shoebox/pipelines/tcdb_search.py` | The interactive session; `parse_command` is pure and tested |
| `shoebox/pipelines/tcdb_add.py` | `tcdb-add`: reads card lines, resolves each to one card, adds it, prints a summary |
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
   in the session (like `add 3`), or a new pipeline plus CLI subcommand if it
   stands alone (like `tcdb-add`).

Pages that load parts of themselves with JavaScript (the ViewCard collection
box comes from `ViewCard_ajax.cfm`) won't have those parts in a saved copy.
Wait for the element in the live page, the way `add_to_collection` does, and
test the parser against the fragment's HTML.

## Troubleshooting

- **Stuck on "Performing security verification"** — Cloudflare wants a click
  it will not give a bot. Click the checkbox in the Chrome window; the tool
  waits up to 90 s. The clearance is then cached in the profile.
- **"No TCDB login detected within 300s"** — you did not finish logging in.
  Re-run; raise `tcdb.login_timeout_s` if you need longer.
- **Chrome version errors** — undetected-chromedriver reads the installed
  Chrome's major version via `chrome_version`; update Chrome or the
  `undetected-chromedriver` package if the two drift apart.
- **`tcdb-add` says `not_found`** — the title doesn't match TCDB's exactly.
  The detail column lists what the search did return, so copy the right
  title from there, or pass the card's ViewCard link.
- **`tcdb-add` says `no_add_button` or `failed`** — the collection box didn't
  look the way shoebox expects (for example, TCDB changed its markup, or you
  are logged out). Open the saved `tcdb_debug/collection_box_*.html`: it shows
  what the box contained.
- **Profile locked** — a previous Chrome on the same profile is still running.
  Quit it (or delete `data/tcdb_chrome_profile/SingletonLock`).
