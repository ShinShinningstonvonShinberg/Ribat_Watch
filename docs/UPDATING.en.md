# Updating Ribat Watch — Maintenance Guide (English)

> 🌐 **日本語版:** [UPDATING.ja.md](UPDATING.ja.md) ・ Quickstart: [../REFRESH.md](../REFRESH.md)

This is the complete reference for keeping Ribat Watch current: the mosque‑location data,
the political‑representation data, the boundaries, and the app itself. It explains **how each
update happens**, **how to trigger it manually**, **what to review**, and **how to recover when
something breaks**.

If you only need the short version, read [REFRESH.md](../REFRESH.md). This document is the
authoritative, exhaustive one.

---

## Table of contents

1. [Philosophy & guarantees](#1-philosophy--guarantees)
2. [One‑time repository setup](#2-one-time-repository-setup)
3. [Updating the mosque data](#3-updating-the-mosque-data)
4. [Updating the representatives data](#4-updating-the-representatives-data)
5. [Validation & invariants](#5-validation--invariants)
6. [Privacy & security — invariants that must always hold](#6-privacy--security--invariants-that-must-always-hold)
7. [Local development & verification](#7-local-development--verification)
8. [Troubleshooting](#8-troubleshooting)
9. [Updating the code / boundaries / app](#9-updating-the-code--boundaries--app)
10. [File & command reference](#10-file--command-reference)

---

## 1. Philosophy & guarantees

Ribat Watch has **two data systems**, and both follow the same shape:

> **scheduled rebuild → open a diff‑only Pull Request → a human reviews and approves.**
> **Nothing is ever auto‑merged.**

| Data | Source | Script | Workflow | Cadence | Secret needed |
|---|---|---|---|---|---|
| Mosque locations (`app/data/mosques.geojson` + counts on `districts_*`) | Third‑party public Google **My Map** "全国モスクリスト" | `scripts/refresh_mosques.py` | `.github/workflows/refresh-mosques.yml` | Weekly + manual | `SCRUB_RULES_JSON` (required) |
| Representatives (`app/data/reps_*.json`) | Japanese Wikipedia (governors / HR / HC) + Claude+web_search (mayors) | `scripts/reps/refresh.py` | `.github/workflows/refresh-reps.yml` | Governors/HR/HC quarterly, mayors monthly, elections manual | `ANTHROPIC_API_KEY` (mayors only, optional) |

**Why a rebuild‑to‑PR instead of a live scrape?**

* The mosque source is a community‑maintained map that may carry **personal information** in
  free text. The published data is deliberately **scrubbed and generalized**; a live client‑side
  scrape would either re‑expose that PII or require shipping the private denylist to the browser.
* The representatives are **named public officials**. Auto‑publishing scraped changes would put
  unreviewed changes to named‑person data straight onto a public map.

So every change lands as a **PR a human reads first**. The automation's job is to *detect* change
and *prepare* a clean diff — not to publish.

**Fail‑safe behaviors (by design):**

* `refresh_mosques.py` **aborts before writing anything** if the scrub rules are unavailable
  (unless `--allow-no-rules`). A misconfigured CI job fails loudly rather than publishing raw PII.
* If the mosque source is unreachable or returns **0 points**, the run aborts and the existing
  published snapshot is kept (the app never breaks).
* `refresh.py` writes reps to a **candidate file, validates it, and only then** promotes it; on
  validation failure with `--apply` it restores the previous committed data.
* Both pipelines are **Python‑standard‑library only** — no `pip install` on CI.

---

## 2. One‑time repository setup

The Actions will not do anything useful until these are configured **once** in the GitHub repo.

### 2.1 `SCRUB_RULES_JSON` secret (required for the mosque pipeline)

The PII denylist lives in `scripts/scrub_rules.json`, which is **git‑ignored** because the terms
themselves are personal information. CI injects it as a secret:

1. Locally, open `scripts/scrub_rules.json` and copy its **entire contents** (the JSON text).
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**.
3. Name: `SCRUB_RULES_JSON`. Value: paste the JSON. Save.

Without this secret the mosque Action **intentionally fails** (see the guard in the workflow and
`scrub.rules_available()`). This is deliberate: it prevents a run that would publish descriptions
with the denylist silently not applied.

> The generalization stage (URL‑only) still runs even with an empty denylist, but the denylist is
> the first line of defense and must be present. Do not "fix" a failing run by adding
> `--allow-no-rules` in CI.

### 2.2 `ANTHROPIC_API_KEY` secret (optional — mayors only)

Only needed if you want the **monthly mayor (首長) tier** to run in CI. Governors / HR / HC need no
key (they scrape Wikipedia).

1. Repo → **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `ANTHROPIC_API_KEY`. Value: your Anthropic API key.

If it is absent, the monthly job logs "skipping" and exits cleanly; governors/HR/HC still update
quarterly. If you are uncomfortable putting an API key on a public repo's Actions, run the mayor
tier **locally** instead (§4.10) and push the branch by hand.

### 2.3 Workflow permission (required for both)

Both Actions open PRs via `peter-evans/create-pull-request@v6`, which needs:

* Repo → **Settings → Actions → General → Workflow permissions** →
  enable **"Allow GitHub Actions to create and approve pull requests."**

### 2.4 Verifying setup

Trigger each workflow manually once (**Actions → pick the workflow → Run workflow**). On an
unchanged source they should complete green and open **no PR** (no diff). That confirms the
secret, permissions, and runner all work.

---

## 3. Updating the mosque data

### 3.1 How the pipeline works

`scripts/refresh_mosques.py`:

1. **Fetch** the KML from the public My Map (`mid` in `scripts/mosque_source.json`, or the
   `MYMAP_MID` env var, or `--mid`). Uses `urllib`, falling back to `curl` if the local Python has
   no CA roots. KMZ (zipped) is unpacked automatically.
2. **Parse + scrub + generalize** via `parse_kml.parse_kml()` → `scripts/scrub.py`:
   * **Scrub** removes known denylist terms/URL‑fragments (`scrub_rules.json`).
   * **Generalize** keeps only URLs and drops all other free text (see §3.5).
3. **Write** `mosques.geojson`, `mosques.csv`, and `app/data/mosques.geojson` (311 points today).
4. **Recount** the points against the committed full‑resolution boundaries
   (`data/recount/{muni,hr}.geojson.gz`) and write the counts back onto
   `app/data/districts_*.geojson` (geometry untouched). Prefecture/HC counts are derived from the
   municipal codes (2‑digit prefix + 合区 rules), matching `build_districts.py` exactly.
5. **Emit a diff summary** (points added/removed/moved, per‑level count deltas) to stdout and
   `--report`.

On an **unchanged source this reproduces the committed data byte‑for‑byte** (verified 0‑diff).

### 3.2 Automatic (weekly)

Cron `17 21 * * 0` — **Sundays 21:17 UTC ≈ Mondays 06:17 JST**. If the source changed, a PR titled
「【自動】モスクデータ更新…」 is opened on branch `auto/refresh-mosques` with the diff summary in the body.

### 3.3 Manual — GitHub UI

**Actions → 「モスクデータ 定期更新（レビュー用PR）」 → Run workflow.** No inputs. Use this to pull the
latest source on demand.

### 3.4 Manual — local run

```bash
# Live: fetch the current source, scrub, recount, print the diff
python3 scripts/refresh_mosques.py

# Offline: run against a local KML export (no network), write a report file
python3 scripts/refresh_mosques.py --kml mosques_raw.kml --report /tmp/report.md

# Override the source map id for one run
python3 scripts/refresh_mosques.py --mid <MAP_ID>
```

Requires a local `scripts/scrub_rules.json` (or the `SCRUB_RULES_JSON` / `SCRUB_RULES_PATH` env).
The full‑resolution boundaries ship as `data/recount/*.gz`, so no `data/raw/` or `shapely` is
needed for an exact recount. After a local run, `git diff` to see what changed, then commit and
open a PR (do **not** push straight to `main`).

### 3.5 The scrub + generalization policy

The published description of each mosque is reduced to **URLs only**; all other free text
(addresses, working notes, personal names, personal SNS handles, opinions) is dropped. This is the
backstop that catches **new** PII the denylist doesn't know about.

* **Kept by default:** non‑SNS hosts — official sites, news, Wikipedia, maps, images. (This is an
  SNS *denylist*, not a host *allowlist*.)
* **SNS hosts** (Facebook / Instagram / TikTok / X / YouTube / LINE / linktr.ee / note.com …):
  kept **only** if the account's normalized URL is in the **grandfather set** — i.e. it already
  exists in the currently‑published data, or it is listed in `scripts/mosque_social_allowlist.json`.
* **Hardening (audited):** the emitted URL has `user:pw@` userinfo and `#fragment` stripped; the
  grandfather match is query‑sensitive, so appending `?leak=…`/`#…` to an approved account URL is
  rejected (the tampered URL is dropped, not published).
* **Residual limit:** PII placed inside the **path/query of an arbitrary non‑SNS host** is *not*
  removed (stripping queries would break legitimate `?cid=` map links, `?v=` videos, `?fife=`
  images). That path is caught by **human PR review** — such a URL appears alone in the diff and is
  conspicuous.

Because `app/app.js` renders **only `source_url`** in mosque popups (never the description),
URL‑only descriptions cause **zero UI regression**.

### 3.6 Grandfathering a mosque's social account

When a refresh PR shows a **new SNS URL** dropped from a description and you confirm it is the
mosque's own official account:

1. Add its normalized URL to `scripts/mosque_social_allowlist.json`:
   ```json
   { "urls": [ "https://facebook.com/example.mosque" ] }
   ```
   (Use the canonical `host + path` form; scheme, `www.`, query, and trailing slash are normalized
   away for matching. This file is public — list only official mosque accounts, never personal ones.)
2. Re‑run the refresh; the account now survives and appears in the diff. Approve the PR.

Accounts already present in the published data are carried forward automatically, so this file can
start empty.

### 3.7 Adding a denylist term

`scripts/scrub_rules.json` (git‑ignored, and mirrored to the `SCRUB_RULES_JSON` secret) has two
arrays:

```json
{ "remove_phrases": ["<a personal name to strip>"], "remove_url_substr": ["<a url fragment>"] }
```

* Add the term locally, re‑run the mosque refresh, verify the term is gone from the diff.
* **Update the `SCRUB_RULES_JSON` secret in the repo to match** — the secret and the local file
  must stay in sync, or CI will scrub differently from your local run.

### 3.8 Changing the source map

The source `mid` is public and lives in `scripts/mosque_source.json`. To point at a different map,
edit `mid` there, or set the `MYMAP_MID` env var / `--mid` for one run. The KML URL template
(`https://www.google.com/maps/d/kml?mid={mid}&forcekml=1`) is fixed to Google's host.

### 3.9 Re‑districting / boundary changes

The count recount uses committed full‑resolution boundaries. If the **boundaries themselves**
change (a municipal merger, an HR re‑districting), regenerate them (see `data/recount/README.md`):

```bash
bash scripts/fetch_raw.sh                     # download data/raw/muni.geojson + senkyoku2022.zip
./.venv/bin/python scripts/build_hr.py        # dissolve → data/raw/hr.geojson (289 HR districts)
gzip -c data/raw/muni.geojson > data/recount/muni.geojson.gz
gzip -c data/raw/hr.geojson  > data/recount/hr.geojson.gz
```

For a display‑geometry change (new simplified `districts_*.geojson`), run the full rebuild
`scripts/build_districts.py` (needs `.venv` with shapely). Commit only the `.gz` files from
`data/recount/`; `data/raw/` is git‑ignored (large, re‑downloadable).

### 3.10 Reviewing a mosque‑data PR — checklist

* **Added / removed points** look plausible (real mosques, not scrape noise).
* **No free‑text PII** in any changed description — descriptions must be **URLs only**. A
  non‑URL string or a `user:pw@` / `?note=<name/address/phone>` URL is a **red flag** → reject.
* **New SNS URLs** are official mosque accounts before grandfathering (§3.6).
* **Count deltas** per level are consistent with the point changes.
* The 2 permanently‑unassigned points (Odaiba / Big Sight prayer rooms, offshore of the simplified
  coastline) are expected — `muni/pref/hc = points − 2`.

---

## 4. Updating the representatives data

### 4.1 How the pipeline works

`scripts/reps/refresh.py` per level:

1. **Collect** the current office‑holders (a scraper or the mayor collector).
2. **Conservative merge** against the committed data: only when the **office‑holder's name
   changes** is a record flagged and replaced; identical names keep the existing (curated) record
   untouched. This preserves hand‑added fields (endorsers, first‑election dates, notes).
3. **Validate** the candidate with `validate.py` (gate).
4. **Diff report** of the change candidates. With `--apply`, promote to `app/data/reps_*.json`.

The committed `reps_*.json` are **static, hand‑curated snapshots**; the scrapers only detect *who
holds office now*, which is the volatile part.

### 4.2 The four levels

| Level | Records | Source | Script | Cadence |
|---|---|---|---|---|
| `pref` — governors | 47 | Wikipedia「都道府県知事の一覧」 | `scrape_pref.py` | Quarterly + elections |
| `hr` — House of Reps (SMD) | 289 | Wikipedia「第51回衆議院議員総選挙」小選挙区当選者 | `scrape_hr.py` | Quarterly + elections |
| `hc` — House of Councillors (districts) | 45 districts / 148 members | Wikipedia「参議院議員一覧」選挙区選出議員 | `scrape_hc.py` | Quarterly + elections |
| `muni` — mayors (首長) | 1,893 | Claude + web_search | `mayors/run.py` | Monthly (key) + elections |

All keyed by the **same `code`** as the district GeoJSON. HC values are member arrays; 合区
(Tottori‑Shimane `31_32`, Tokushima‑Kōchi `36_39`) share a code.

### 4.3 Automatic (quarterly / monthly)

* **Quarterly** cron `23 20 1 1,4,7,10 *` (1st of Jan/Apr/Jul/Oct, 20:23 UTC): governors + HR + HC
  (`--level all --skip-muni`).
* **Monthly** cron `23 20 5 * *` (5th, 20:23 UTC): mayors only — and only if `ANTHROPIC_API_KEY`
  is set (otherwise it logs and exits cleanly).

Each opens a PR 「【自動】議員データ更新（現職の変化）」 on branch `auto/refresh-reps`.

### 4.4 Manual — GitHub UI (elections)

**Actions → 「議員データ 定期更新（レビュー用PR）」 → Run workflow**, with inputs:

* `level` = `pref` | `hr` | `hc` | `muni` | `all` (default `all`)
* `code` = a single district code to narrow to (optional; for `muni` a 2‑digit prefecture prefix)

Inputs are validated (level against the allow‑list, code reduced to digits/underscore) and passed
as environment variables — safe against shell injection.

### 4.5 Manual — local run

```bash
python3 scripts/reps/validate.py                          # check current data's invariants
python3 scripts/reps/refresh.py --level pref              # DRY RUN: show diff, write only a candidate
python3 scripts/reps/refresh.py --level all --skip-muni   # governors/HR/HC (no key needed)
python3 scripts/reps/refresh.py --level pref --code 47    # just Okinawa governor
python3 scripts/reps/refresh.py --level pref --apply      # write app/data/reps_pref.json (CI uses this)
```

* **Without `--apply`** the merged result goes to `reps_<level>.candidate.json` (git‑ignored) and
  is validated there — a safe preview.
* **With `--apply`** it writes the real file, validates it, and rolls back to the committed version
  if validation fails.
* On an unchanged roster all levels print **変更なし** (verified against live Wikipedia).

### 4.6 Election runbook (worked example)

**沖縄県知事選 2026‑09‑13** (prefecture code `47`). After the result is official:

*Via GitHub:* Actions → refresh‑reps → Run workflow → `level=pref, code=47`. Review the PR
(前任 → 新任), confirm the source, merge.

*Locally:*
```bash
python3 scripts/reps/refresh.py --level pref --code 47            # preview the turnover
python3 scripts/reps/refresh.py --level pref --code 47 --apply    # apply, then git diff / open PR
```
The auto‑detected record is minimal (name + source) with a note that curated fields (endorsers,
first‑election date, exact term end) are **未補完** — fill those in by hand before/after merging
(§4.12).

Analogous triggers: any gubernatorial by‑election, an HR general election (bump the source article
in `scrape_hr.py` and re‑verify the color→party map), an HC regular election (2028).

### 4.7 The conservative merge

Change detection compares `ident(name)` (see §4.8), not the whole record. Consequences:

* A pure **name change** at a code → flagged `turnover`, record replaced with a minimal stub + note.
* A **new code** (new district) → flagged `new_code`.
* Same name → **record kept verbatim** (curation preserved). Non‑name fields (party, term, kana)
  are **not** auto‑updated — update those by hand if a source corrects them.

### 4.8 Name variants & kanji folding

Two sources can spell the same person differently. Matching goes through `ident()` =
`norm_name` + a 旧字体/異体字 fold table + an alias bridge:

* **`scripts/reps/name_aliases.json`** — groups of "same person, different spelling", e.g.
  `["塩村あやか", "塩村文夏"]`. The **first entry is the spelling used in the published data.** When a
  refresh shows a **false turnover** (a phantom "A → B" that is really one person under a
  campaign/kana name vs. a formal kanji name), add the pair here and re‑run.
* **`_KANJI_FOLD`** in `_common.py` folds common variant characters (壽↔寿, 邊↔辺, 髙↔高 …) for
  *comparison only*; display names are kept as scraped/curated. If a genuinely new variant pair
  causes churn, either add it to the fold table or (preferred for a specific person) to
  `name_aliases.json`.

### 4.9 Vacancies (欠員)

A seat with no current holder (a resignation with no by‑election) is a **first‑class member entry**:

```json
{ "name": null, "vacant": true, "cls": "2028", "termEnd": "2028-07-25",
  "note": "…resigned …; vacant until 2028", "sourceUrl": "https://…" }
```

It **counts toward the seat invariants** (148 total, 74/74 class split, Tokyo = 12). `validate.py`
and `scrape_hc.py` exempt vacant entries from the name/party requirement, and `app.js` renders it as
「（欠員）」 with the header 「（N議席・M欠員）」. To model a new vacancy, replace the departed member's
object with the shape above.

### 4.10 The mayor / agent collector

Mayors are too numerous and scattered for a deterministic scraper, so `scripts/reps/mayors/run.py`
asks Claude (Messages API, model `claude-opus-4-8`, tool `web_search_20260209`) "who is the current
head of each municipality" per prefecture, and fills the deterministic parts (政令市 administrative
wards inherit the parent‑city mayor; 北方領土 six villages are `noHead`) from `targets.py`.

```bash
python3 scripts/reps/mayors/targets.py                     # show the target classification (1712+175+6=1893)
python3 scripts/reps/mayors/run.py --dry-run --pref 47     # generate the prompt only (no key)
python3 scripts/reps/mayors/run.py --pref 47 --out /tmp/okinawa.json   # real collection (needs key)
```

* Needs `ANTHROPIC_API_KEY`. It is **opt‑in and comparatively expensive** (a nationwide run is one
  prompt per prefecture + one for the 政令市). Model/web output is **sanitized** (angle brackets
  stripped, URL scheme checked) and unconfirmed municipalities keep `name: null` and fall back to
  the prior record — never guessed.
* If you would rather not put an API key on CI, run this locally, `--apply` through `refresh.py`,
  and push the branch by hand.

### 4.11 Reviewing a reps PR — checklist

* **Every flagged change is a real office‑holder change** — cross‑check the source link.
* **No false turnovers** from spelling variants (if any, add to `name_aliases.json` — §4.8).
* Curated fields on a turnover stub are back‑filled (endorsers, first‑election date, term end).
* `validate.py` is green (the CI job blocks on it; confirm locally too).
* For mayors, spot‑check a few `sourceUrl`s and dates; treat `name: null` as "unconfirmed, kept".

### 4.12 Hand‑editing a single record (correction)

For an off‑cycle fix (a typo, a source correction), edit `app/data/reps_<level>.json` directly and
**run `validate.py` before committing**:

```bash
# edit the record …
python3 scripts/reps/validate.py --level hr
```

Keep the shape consistent with neighbors (all required fields present, `sourceUrl` http(s), no
`<`/`>`). Bump the file's `asOf` if you like, then commit + PR.

---

## 5. Validation & invariants

`scripts/reps/validate.py` is the gate every reps change must pass:

```bash
python3 scripts/reps/validate.py                 # all levels (exit 0 = pass, 1 = errors)
python3 scripts/reps/validate.py --level hc
python3 scripts/reps/validate.py --data-dir DIR  # validate a candidate directory
```

What it checks:

* **Structure:** `{level, office, asOf, records}` present; `asOf` is a date.
* **Magnitudes:** 47 governors; 289 HR (codes match `districts_hr`); 45 HC districts / **148
  members total**, 74/74 class split, Tokyo = 12, 合区 codes `31_32`/`36_39` present; mayors within
  1700–2000.
* **Code alignment:** reps keys match the district GeoJSON code set (mayors ⊆).
* **Required fields** per level (vacancies and no‑head municipalities exempted appropriately).
* **Party colors:** every referenced party has a color in `reps_parties.json` (warning).
* **Injection scan (security):** rejects any record string containing `<`/`>` and any `sourceUrl`
  whose scheme is not http/https — this stops HTML/script payloads (relevant to the agent‑collected
  mayor data) from reaching the app, which renders into `innerHTML`.

Warnings (e.g. a year‑only `termEnd`) do not block; errors return exit 1 and block the pipeline.

---

## 6. Privacy & security — invariants that must always hold

**Never commit / never publish** (all git‑ignored — verify with `git status --ignored`):

* `scripts/scrub_rules.json` — the PII denylist (itself PII; lives only locally + as a secret).
* `mosques_raw.kml` — the un‑scrubbed source export.
* `data/raw/` — large boundary sources (re‑downloadable).
* `Export_Mega/`, `funding_work/` — private research.
* Pipeline transients: `app/data/reps_*.candidate.json`, `*.scraped.json`,
  `scripts/reps/mayors/_prompts/`, `refresh_report.md`.

**Data invariants:**

* Published mosque descriptions are **URLs only** — no addresses, names, notes, or personal SNS.
* Only **named public officials** appear in reps data; **mosque‑side individuals are never named.**
* `app/app.js` renders mosque **`source_url` only**, never the description. If you ever start
  rendering descriptions, revisit the whole scrub policy.

**App hardening (keep it):**

* `app.js` passes every data‑derived string through `esc()` (HTML‑escape) and every link through
  `safeUrl()` (http/https only) before it hits `innerHTML`. New popup/table fields must do the
  same — the mosque `name` comes from a third party and mayor fields come from an LLM.

**Identity:** commits use the GitHub‑noreply identity; no real name/email/local path in the repo.

---

## 7. Local development & verification

The app is **static** — no build step.

```bash
# Run the app
python3 -m http.server 8777      # then open http://localhost:8777/app/
# or, on macOS, double‑click serve.command

# Reproduce the mosque data from the raw KML and confirm 0‑diff
python3 scripts/refresh_mosques.py --kml mosques_raw.kml
git diff --stat                  # should be empty on an unchanged source

# Reproduce reps against live Wikipedia and confirm no changes
python3 scripts/reps/refresh.py --level all --skip-muni   # expect 変更なし

# Self‑test the scrub logic (including the anti‑tamper cases)
python3 scripts/scrub.py
```

Environment notes: everything is **Python standard library** (Python 3.12 on CI, 3.14 works
locally). On macOS the local Python may lack CA roots, so the pipelines fall back to `curl` for
HTTPS — do **not** disable TLS verification to work around it.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Mosque Action fails immediately with a `SCRUB_RULES_JSON` error | The secret is unset/empty | Set the secret (§2.1). Do **not** add `--allow-no-rules` in CI. |
| Mosque run aborts "地点が0件" | Source map unreachable / emptied / made private | It fails safe (keeps the snapshot). Re‑run later; if the map moved, update the `mid` (§3.8). |
| `! 警告: … 書き戻せません` on recount | A point fell into a boundary that only exists in the simplified geometry (boundary changed) | Regenerate `data/recount/*.gz` (§3.9), or do a full `build_districts.py` rebuild. |
| Mosque PR shows unexpected free text in a description | A new PII vector slipped past generalization | Reject the PR; add the term to `scrub_rules.json` (§3.7) and re‑run. |
| Scraper raises `RuntimeError: …構造変化の可能性` | The Wikipedia article structure changed | Update the parser (section header, template name, or `_COLOR_PARTY`); re‑verify magnitudes. |
| HR scraper raises "未知の政党色コード" | A party's box color isn't in `_COLOR_PARTY` | Add the verified `hex→party` mapping in `scrape_hr.py`. |
| Reps PR shows a "turnover" that is the **same person** | Spelling/kana/kanji variant | Add the pair to `name_aliases.json` (§4.8) and re‑run. |
| `validate.py` errors on member count / class split | A row was dropped or a vacancy mis‑modeled | Compare the scraped set to the roster; model vacancies per §4.9. |
| `validate.py` "出典URLが http/https ではありません" or "HTMLになりうる文字" | A record has a bad URL scheme or angle brackets (often from the mayor collector) | Fix the offending record; the injection scan is intentional. |
| Monthly reps job did nothing | `ANTHROPIC_API_KEY` not set | Expected — set the key (§2.2) or run mayors locally (§4.10). |
| Count diffs appear even though source is unchanged | Recount ran on **simplified** geometry (the `.gz` were missing) | Ensure `data/recount/*.gz` are committed and present. |

---

## 9. Updating the code / boundaries / app

* **The app** (`app/`) is static; there is no bundler. Edit `app/app.js` / `index.html` /
  `style.css` directly. `data/*.geojson` and `reps_*.json` are the only inputs. **Publishing** =
  merge to `main`, then serve the `app/` directory from any static host (e.g. GitHub Pages pointed
  at the repo — no deploy workflow is required or present).
* **Boundaries** are built by `scripts/build_hr.py` (dissolve the CC0 CSIS shapefile → 289 HR
  districts) and `scripts/build_districts.py` (spatial‑join the points + simplify → `app/data/*`).
  These need the `.venv` (shapely/pyshp); `scripts/geo.py` is a dependency‑free fallback. Only run
  these when the **boundaries** change; day‑to‑day count updates go through `refresh_mosques.py`.
* **Adding a district level** would touch `DISTRICT_FILES`/`LEVEL_LABEL` in `app.js`, a new
  `districts_<x>.geojson`, and (if it has representatives) a new `reps_<x>.json` + a `validate.py`
  checker + a scraper.

---

## 10. File & command reference

**Update pipeline files**

```
scripts/scrub.py                         PII scrub + generalization (shared by parse_kml & refresh_mosques)
scripts/refresh_mosques.py               fetch → scrub → recount → diff (weekly Action)
scripts/mosque_source.json               source My Map mid (public)
scripts/mosque_social_allowlist.json     grandfathered mosque SNS accounts (public; you edit)
scripts/scrub_rules.json                 PII denylist (LOCAL ONLY / secret; never committed)
data/recount/{muni,hr}.geojson.gz        full‑res boundaries for exact recount (committed)
scripts/reps/validate.py                 reps invariant + injection checks (the gate)
scripts/reps/_common.py                  wiki fetch, ident/merge, kanji fold, write
scripts/reps/scrape_{pref,hr,hc}.py      governors / HR / HC scrapers (Wikipedia)
scripts/reps/name_aliases.json           same‑person spelling bridges (you edit)
scripts/reps/refresh.py                  collect → merge → validate → diff (orchestrator)
scripts/reps/mayors/{targets,run}.py     agent‑assisted mayor collector (+ collect_prompt.md)
.github/workflows/refresh-*.yml          the two scheduled → PR Actions
```

**Command cheatsheet**

```bash
# Mosque
python3 scripts/refresh_mosques.py [--kml FILE] [--mid ID] [--report FILE] [--allow-no-rules]

# Reps
python3 scripts/reps/validate.py [--level pref|hr|hc|muni|all] [--data-dir DIR]
python3 scripts/reps/refresh.py --level LEVEL [--code CODE] [--apply] [--skip-muni] [--report FILE]
python3 scripts/reps/mayors/targets.py [--pref NN]
python3 scripts/reps/mayors/run.py [--dry-run] [--pref NN] [--out FILE]

# App
python3 -m http.server 8777    # → http://localhost:8777/app/
```

---

*Golden rule: the automation prepares a diff; a human publishes it. When in doubt, don't merge —
open a question on the PR.*
