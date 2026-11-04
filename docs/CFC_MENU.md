# CFC menu in the POS

Source: `CFC_Menu_Food_and_Drinks.pdf` (City Food Court – House of Mandi). Every page was transcribed into
`scripts/build_cfc_menu_csv.py`, which writes `menus/cfc_menu.csv`. The CSV is what the POS loads.

```
python scripts/build_cfc_menu_csv.py                       # rebuild the CSV after editing the script
python scripts/load_menu.py --cfc --hide-missing           # load it (or double-click scripts\windows\load_cfc_menu.bat)
python scripts/load_menu.py --preview                      # check only, nothing saved
```

The same import is in **Setup → Import / export → Menu (CSV)**, with *Check file* first. Loading again is safe:
unchanged items are left alone, price changes are listed and audited, and nothing is ever deleted
(`--hide-missing` hides items that are not in the file, e.g. the demo menu).

## What is on the till

| | |
|---|---|
| Menu rows | **328** in 25 categories |
| On sale | **325** |
| Hidden until a price is set | 3 (Make your own combo, Black iced, Grape smoothie) |
| Major groups | Food 181 · Beverage 109 · Extras 27 · Included sides 11 |

* **Options as one button.** Items printed with sizes or choices (pizza Regular/Medium/Large/Mega, coffee
  Single/Double, cup/pot, samosa Beef/Chicken/Veg …) are one tile; tapping it opens a picker. Each option is its
  own menu item underneath, so reports, prices and stock links stay per option.
* **Descriptions** from the menu (e.g. what a combo includes) show on the picker and in Setup → Menu.
* **Sale days.** The Thursday pizza special (2 Large 1200 / 2 Medium 900) can only be sold when the business day is a
  Thursday; the server rejects it on other days, even if the screen was left open.
* **Accompaniments** printed as "served with … (choice of)" are zero-price items in the *Included sides* group
  ("With – Pilau rice", "With – Fries" …). Servers add the customer's choice so the kitchen ticket is right; they
  never change the total. Only *Included sides* items (or hidden items) may have price 0.
* **Add-ons** (extra chapati, extra cheese, sauces …) are normal priced items in the *Extras* group.
* **Notes** (no onions, well done, …) are typed per line and print on the kitchen ticket and receipt.

## VAT: prices already include it

The menu states prices are inclusive of VAT. The CFC profile (`--cfc`) sets **VAT 16 %, included in prices**:

* the customer pays exactly the menu price; nothing is added on top;
* the receipt shows the VAT portion inside the total: VAT = price × 16 / 116, labelled "incl. VAT 16%";
* reports show net sales ex VAT and the VAT collected separately.

Worked example (tested): Butter chicken 800 + With – Pilau rice 0 + 2 × Soda 200 = **1000.00** total, of which
VAT 137.93, net 862.07.

**Not included:** KRA eTIMS. The POS does not sign or transmit invoices to KRA, and receipts say they are not
fiscal receipts. If CFC must issue eTIMS invoices, keep the existing eTIMS device/process until an integration is built.

## Price decisions

Where the PDF disagrees with itself, the owner's decision (29 Oct 2026) is to **use the lower price**:

| Item | Printed | On the till |
|---|---|---|
| Paneer masala | 600 (Vegetarian page) and 680 (Pasta & Rice page) | one item, **600** |
| Black coffee | Single 200, Double 180 | Single **180**, Double **180** |
| English tea | Cup 200, Pot 200 | Cup 200, Pot 200 (as printed) |
| Berry Bliss (new) | 500 (the Berry bliss milkshake is 400) | **400** |

Still hidden because the menu prints **no price at all** (marked *Needs review* in Setup → Menu):
Make your own combo, Black iced, Grape smoothie. Set a price in Setup → Menu and tick *Active* to sell them.

## Notes from the transcription

* Butter chicken and Chicken tikka (boneless) appear on two pages at the same price: loaded once.
* "Chicken biryani" and "Biryani – Chicken" are the same dish at the same price: loaded once as an option of Biryani.
* "Esbesso chapati" (Breakfast) is kept spelled exactly as printed; rename it in Setup → Menu if it is a typo.
* Resale items are linked 1:1 to stock using the names from the stock-movement workbook: Soda → sodas,
  Dasani/Keringet 500 ml and 1 L, Chapati, Mandazi, Samosas (2 pcs) → 2 × samosa. Cooked dishes need recipes
  (Setup → Import / export → recipe template) before their ingredients are deducted.
