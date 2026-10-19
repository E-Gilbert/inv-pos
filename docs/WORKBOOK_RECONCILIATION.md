# Workbook reconciliation — "oct 2026 stock movement.xlsx"

Analysis of CFC's daily stock-movement workbook (10 sheets, 1–12 October 2026) by the importer in `app/workbook.py`.
The workbook was only read, never modified. Re-run any time: Setup → Import / export → *Analyse workbook*.

## How the workbook maps to INV-POS

| Workbook column | Formula in workbook | INV-POS field |
|---|---|---|
| B item | – | stock item (`ingredients`) |
| C opening | typed (often `=a+b+c` for counts in several places) | `day_stock.opening` |
| D additions | typed | `delivery` movement → `day_stock.delivered` |
| E closing | typed (physical count) | `day_stock.counted` |
| F actual sales | `= C + D − E` (physical usage) | opening + delivered − counted |
| G system sales | typed from the other (Oracle) POS | `external_sale` movement → `day_stock.sold` |
| H variance | `= G − F` | `day_stock.variance = counted − expected` (**identical value and sign**) |
| I note | free text | `day_stock.reason` ("Workbook note: …") |

Imported sheets become closed, read-only days (`source = workbook`). The last closing (12th) becomes the opening stock offered when the next day is opened. The 9th–12th history also feeds the reorder suggestions.

## Integrity checks: all passed
* All `actual sales` and `variance` formulas are correct (recomputed for every row: 0 mismatches).
* **Every opening equals the previous sheet's closing** (0 carry-over breaks), including across the missing 6th.
* No duplicate items, text in number cells, negative usage or missing closing counts.

## Material discrepancies (need a business answer, not fixed automatically)

1. **Units are not stated anywhere.** The importer suggests a unit (all whole numbers → pieces; meat/rice → kg; oil/milk → L) and nothing is saved until every unit is confirmed. Check especially:
   * **shakes** — whole-number counts (124, 111…) but fractional POS sales (2.25, 10.75). It can't be pieces, and it isn't clear what is being counted. **No suggestion is given; you must decide.**
   * **wheat flour / maize flour** — whole numbers (48 added at a time), so they're suggested as pieces (packets). If they are kg, choose kg.
   * **chicken** — 285 in stock with ~20 used a day: pieces/portions, not whole birds?
2. **Day 6 has no sheet**, and **"4th–5th" is one sheet for two days.** Day 4–5 is imported as a single period (2026-10-04 → 05); daily figures for those days can't be separated. Nothing was lost because the 5th closing equals the 7th opening.
3. **The POS sold more than physically left the store on 27 occasions** (positive variance). That's impossible unless a delivery wasn't written down, a count is wrong, or another item was rung up instead:

| Sheet | Item | POS sold | Left the store | Over |
|---|---|---|---|---|
| 1st | shakes | 2.25 | 0 | +2.25 |
| 1st | keringet 1/2 | 7 | 4 | +3 |
| 1st | keringet 1ltr | 8 | 7 | +1 |
| 1st | barista milk | 10 | 8 | +2 |
| 2nd | keringet 1/2 | 8 | 7 | +1 |
| 2nd | dasani 1 ltr | 1 | 0 | +1 |
| 3rd | beef steak | 2.4 | 2.28 | +0.12 |
| 3rd | samosa | 30 | 29 | +1 |
| 3rd | shakes | 10.75 | 9 | +1.75 |
| 4th-5th | raw goat meat | 12.63 | 9.39 | +3.24 |
| 4th-5th | chapati | 32 | 31 | +1 |
| 4th-5th | mandazi | 7 | 5 | +2 |
| 4th-5th | sodas | 22 | 21 | +1 |
| 7th | samosa | 10 | 8 | +2 |
| 7th | barista milk | 15.6 | 12.5 | +3.1 |
| 8th | chicken | 20 | 19 | +1 |
| 8th | samosa | 90 | 87 | +3 |
| 8th | keringet 1ltr | 13 | 12 | +1 |
| 9th | barista milk | 16.8 | 15 | +1.8 |
| 10th | beef steak | 1.56 | 1.5 | +0.06 |
| 10th | shakes | 4.25 | 4 | +0.25 |
| 10th | cooking oil | 8 | 7.95 | +0.05 |
| 11th | shakes | 3.75 | 2 | +1.75 |
| 12th | chapati | 6 | 3 | +3 |
| 12th | shakes | 3 | 2 | +1 |
| 12th | sodas | 6 | 5 | +1 |
| 12th | cooking oil | 5 | 4.8 | +0.2 |

4. **Waste was booked as variance.** Chapati/mandazi that "overstayed" were never recorded as waste, so they show as unexplained shortages (chapati −29 over the period). In INV-POS, record it under Stock → *Record waste* with the reason *Overstayed / expired*.
5. **Mis-keyed items.** Dasani sold as Keringet (1st), and "barista" sodas (10th). These cancel out between items but make both look wrong. Menu items with their own barcodes/SKUs prevent this.
6. Notes recorded in the workbook:

| Sheet | Item | Note |
|---|---|---|
| 1st | chapati | overstayed |
| 1st | dasani 1 ltr | sold as keringet |
| 1st | dasani 1/2 | sold as keringet |
| 2nd | chapati | overstayed |
| 2nd | mandazi | overstayed |
| 10th | chicken Africana | used as a complimentary over a samall portion complain. |
| 10th | sodas | barista |

## Period summary per item (workbook units)

| Item | Suggested unit | Added | Used (physical) | POS sold | Variance (sold − used) | Days with a variance |
|---|---|---|---|---|---|---|
| raw goat meat | kg | 63 | 63.58 | 57.91 | -5.67 | 10 |
| beef steak | kg | 16.5 | 18.9 | 16.48 | -2.42 | 10 |
| fish | pcs | 37 | 41 | 38 | -3 | 2 |
| chicken | pcs | 276 | 224 | 224 | +0 | 2 |
| chicken Africana | pcs | 0 | 21 | 20 | -1 | 1 |
| chapati | pcs | 213 | 207 | 178 | -29 | 7 |
| mandazi | pcs | 63 | 65 | 59 | -6 | 4 |
| samosa | pcs | 293 | 291 | 294 | +3 | 6 |
| shakes | **must choose** | 104 | 46 | 50.23 | +4.23 | 10 |
| sodas | pcs | 240 | 168 | 157 | -11 | 6 |
| keringet 1/2 | pcs | 120 | 70 | 68 | -2 | 4 |
| keringet 1ltr | pcs | 156 | 142 | 140 | -2 | 4 |
| dasani 1 ltr | pcs | 24 | 8 | 8 | +0 | 2 |
| dasani 1/2 | pcs | 24 | 12 | 7 | -5 | 3 |
| cooking oil | L | 120 | 89.9 | 87 | -2.9 | 9 |
| wheat flour | pcs | 144 | 86 | 86 | +0 | 0 |
| maize flour | pcs | 96 | 54 | 54 | +0 | 0 |
| rice | kg | 75 | 73.55 | 68 | -5.55 | 9 |
| kitchen milk | L | 12 | 13.5 | 13.5 | +0 | 0 |
| barista milk | L | 156 | 156.5 | 158 | +1.5 | 10 |

Negative = more left the store than the POS recorded (shortage). Largest by quantity: chapati −29, sodas −11, mandazi −6, goat −5.67 kg, rice −5.55 kg. Costs are not in the workbook, so these can't be valued until purchase costs are set in INV-POS.

## Decisions taken
* Imported values are stored exactly as recorded (no corrections), with workbook notes kept as reasons.
* History can only be imported **before** any day already in the system, and the same file can't be imported twice (SHA-256 check).
* Items are matched to existing stock items by name (case-insensitive). If the units differ, the import stops rather than converting silently.
* Imported days carry no money (sales were on the other POS), so the sales reports show 0 for them; stock reports include them.
