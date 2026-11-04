# Feature parity with the current MICROS RES 3700 back office

CFC's existing POS is Oracle MICROS RES 3700 ("Autosequences and Reports"). It has no inventory layer.
This table lists its core report menu and where the same information lives in INV-POS (Reports → Reports).
Every report works for one day ("daily") or any date range ("consolidated"), and can be printed or exported to CSV.

| MICROS button / report | INV-POS report | Notes |
|---|---|---|
| Sales Balance → Sales (Daily/Consolidated SYS & RVC Sales Detail) | **Sales detail** | Gross, discounts, tax, returns, net sales, collected, checks, average check, guests, average per guest, voids; tender media; order types; one line per day |
| Sales Balance → Tax | **Tax** | By rate, tax on refunds deducted, net tax. Not KRA eTIMS |
| Sales Balance → Tips | – | No tips are recorded at CFC's tills today |
| Period → Time Period | **Time period (hourly)** | By hour the check was paid |
| Period → Serving Period | **Serving period** | Periods set in Settings (default Breakfast 05–11, Lunch 11–16, Dinner 16–05) |
| Cash Management | **Cashier shifts (over / short)** | Float, cash sales, cash refunds, expected, counted, over/short, M-Pesa and card per shift |
| Guest Check | **Open checks** (POS) + **Guest check journal** | Servers open checks per table, add rounds, send to the kitchen (ticket prints) and the cashier settles later. The journal lists every check with table, server, items, type, guests, discount, tax, payment and status |
| Employee → Employee (incl. "40Col Employee Detail, Order Types and Tracking", Employee Detail Totals) | **Employee sales & order types** | Checks, guests, gross, discounts, net, average check, voids, refunds given, discounts approved; split by order type |
| Employee → Cashier | **Cashier shifts** | As above |
| Group → Major Group | **Major group** | Major group is set per category (e.g. Food, Beverage) |
| Group → Family Group / Menu Item Group | **Category (family group)** | Qty, gross, discounts, returns, net, % of net |
| Menu Item | **Menu item sales & food cost** | Same plus food cost, food cost % and gross margin per item, which MICROS can't show |
| Daily Revenue Center Discounts (Detail) | **Discounts** | By reason and per check, with who approved |
| Return/Void Reason Code | **Voids** | By reason, who rang and who voided; plus items removed from open checks after being sent to the kitchen (reason, approver) |
| Refund Reason Code | **Refunds / returns** | By reason, original check, method, restocked or not, approver |
| Autosequences | **Print end-of-day pack** button and `scripts/eod_reports.py` | The script writes CSVs + a printable HTML pack to `data/reports/<date>/`; schedule it with Task Scheduler |
| Labor, KDS, Table Management, GSS | – | Not in scope: no time clock, kitchen display screen or table plan. Kitchen tickets print from the browser |

## The other MICROS tools at CFC

| MICROS tool | INV-POS | Gaps |
|---|---|---|
| **Control Panel / "reset"** (restart when the system or a till freezes) | Automatic: a watchdog checks the server every 2 minutes and restarts it if frozen or stopped. Manual: `scripts\windows\restart_server.bat`. Paid sales are saved instantly and open checks survive a restart | A frozen till is a browser page: reload it; logged-in staff and unsent cart items survive the reload |
| **End of day / clear totals** (autosequence) | **Close day** in the app (counts, reasons, day report). Totals are never cleared: each business day is kept and reports run for any date range. Nightly backup 23:30 and end-of-day pack 23:45 run on their own | None |
| **POS Configurator: menu items, prices, family/major groups** | Setup → Menu, Categories (major group), menu CSV import/export with preview and audited price changes | No touchscreen layout editor (tiles follow category and sort order); no price levels (e.g. happy hour); no forced condiment prompts (options pickers and notes cover CFC's menu) |
| **POS Configurator: employees, privileges** | Setup → Staff with 4 roles; manager PIN approvals | No time clock |

What INV-POS adds that MICROS doesn't have at CFC: the whole stock layer (recipes deduct ingredients, deliveries, waste, closing counts with reasons, purchase orders, stock colours, valuation, cost of goods and gross profit).

Definitions are in `app/reports.py`. Net sales always exclude tax, are after discounts, and subtract refunds on the day the refund was given. Voided checks are never counted as sales.
