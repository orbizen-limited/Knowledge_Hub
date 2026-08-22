# `validate_topic_v9.py` — Quick Reference

Companion to `vd_knowledge_hub_prompt_v9.md`.
Editorial owner: **Dr. A.F.M. Helal Uddin, MRCP(UK), FRCP(London)**, Associate Professor of Medicine, Sir Salimullah Medical College, Dhaka, Bangladesh.

Standard library only — no `pip install` needed. Python 3.9+.

---

## 1. The four layers

| Layer | Catches | Needs network? |
|---|---|---|
| **Structural** | missing/forbidden keys, bad enums, empty required fields, non-ASCII punctuation | No |
| **Evidence** | dangling `[N]`, unused refIds, missing society guideline, `[EC:n]` integrity + quota, fake DOI format | No |
| **Density** | points < 200 chars, points with no numeric, n-gram padding, duplicated points, generic headings | No |
| **Network** | DOIs that don't resolve on CrossRef, media URLs that 404 | Yes (opt-in) |

---

## 2. Commands

```bash
# Fast local validation
python validate_topic_v9.py topic.json

# Full run: network verification + machine-readable report + repair prompts
python validate_topic_v9.py topic.json \
    --media-mode VERIFIED \
    --check-dois --check-media --mailto editorial@doctorshero.com \
    --json-report report.json --emit-repair-prompts --verbose

# Merge a 4-pass generation (PART 7 of the prompt), then validate
python validate_topic_v9.py --merge p1.json p2.json p3.json p4.json -o merged.json
python validate_topic_v9.py merged.json

# Normalise smart quotes / non-breaking hyphens before storing
python validate_topic_v9.py topic.json --autofix-ascii -o clean.json

# Regression-test the validator itself
python make_fixtures.py && python validate_topic_v9.py fixture_bad.json --media-mode VERIFIED
```

### Flags that matter

| Flag | Default | Effect |
|---|---|---|
| `--reference-mode` | `R-STRICT` | `R-DRAFT` **forbids** DOIs (they'd be fabrications) and permits `PENDING VERIFICATION` stubs |
| `--media-mode` | `QUERY-ONLY` | `QUERY-ONLY` **forbids** `proposedUrl` and requires `searchDirectives`; `VERIFIED` requires `verificationStatus: "fetched-ok"` |
| `--extensions` | `on` | `off` skips all v9 additive checks (V17) for v8 back-compat |
| `--min-chars` | `100000` | Lower it while drafting |
| `--strict` | off | Treat warnings as failures |

**Exit codes:** `0` pass · `1` fail · `2` usage/IO error. Wire into CI directly.

---

## 3. The repair loop — where quality actually comes from

Regenerating a 100k-character topic because two keys failed is wasteful. `--emit-repair-prompts` groups every ERROR by its `repair_key` and prints a ready-to-paste re-prompt:

```
### Regenerate `managementSections` (9 error(s))

Regenerate ONLY the key `managementSections`. Emit a JSON object containing that
key and nothing else. Reuse the existing refId numbering exactly; do not introduce
new references. It failed validation for these reasons:
  - V9_BLOCK_COUNT: 3 management blocks < required 7
  - V12_BLOCK_NO_CITATION: Block '...' contains no [N] or [EC] marker
  - F7_PADDING: 49% of 8-grams repeated (cap 25%)
```

Full pipeline:

```
1. Pass 0        lock the reference pack; spot-check 3 DOIs by hand
2. Passes 1-4    generate
3. --merge       deep-merge into one document
4. validate      get failedFloors + repair keys
5. re-prompt     regenerate ONLY the failing keys
6. --check-dois --check-media    final network verification
7. publish       status = "published", nextReviewDue = +12 months
```

---

## 4. How the two heuristics work

**Numeric density.** Citation markers are stripped first — otherwise `[3]` would count as a dose. Then digits are counted per 120 characters. Target ≥ 1.0. Below that, the text is narrative rather than actionable.

**n-gram padding.** Management text is tokenised, split into 8-word windows, and the fraction of repeated windows computed. Analogy: a student repeating the same eight-word phrase to fill a page scores high; original writing scores near zero.

Measured calibration:

| Text | Ratio | Verdict |
|---|---|---|
| Varied real clinical prose | **0.00** | pass |
| Model padding to hit a floor | **0.30** | fail (cap 0.25) |

The cap sits cleanly between the two. Tune `NGRAM_REPEAT_MAX` at the top of the file if your topics run more repetitive by nature (e.g. drug monographs).

---

**Expert-consensus markers.** `[EC:n]` is checked exactly like a reference: the set of `ecId`s cited in the text must equal the set declared in `expertConsensusClaims[]`, with no duplicates. A bare `[EC]` with no number is rejected. Separately, `[EC:n]` may carry no more than 15% of blocks in any section — beyond that the section is under-sourced, not merely consensus-based. The EC sequence is independent of `refId`, so `[EC:1]` and `[1]` never collide, and EC digits are stripped before the numeric-density scan so they can't masquerade as a dose.

| Failure | Code |
|---|---|
| `[EC]` with no number | `V18_EC_BARE` |
| `[EC:7]` cited, never declared | `V18_EC_DANGLING` |
| `ecId` declared, never cited | `V18_EC_UNUSED` |
| Same `ecId` declared twice | `V18_EC_DUPLICATE` |
| Over 15% of blocks per section | `V18_EC_RATIO` |

## 5. Tuning

All floors live in `DEFAULT_FLOORS` at the top of the file. All rule constants — enums, licence allow-list, foreign-language tokens, generic-heading blacklist, placeholder patterns — sit in **Section 1** in one editable block. Adding a new check means writing one `check_*` method and appending it to the `checks` list in `TopicValidator.run()`; a crash inside any single check is caught and reported rather than hiding the others.

**Cache:** network results are cached in `.v9_net_cache.json` so a repair loop doesn't re-query the same DOI fifty times. Delete it to force a re-check.
