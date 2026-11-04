# Installing INV-POS at City Food Court (Windows server + tills + Android tablets)

Time needed: about 45 minutes, plus staff training. Internet is needed only during step 2.

## What you need
* **Server PC** (Windows 10/11, always on, on a UPS). It can also be a till.
* **Tills / tablets**: any Windows PC or Android tablet/phone with Chrome or Edge, on the **same Wi-Fi/LAN** as the server.
* **Receipt printer** (80 mm) installed in Windows on each till that prints. Optional USB/Bluetooth **barcode scanner** in keyboard mode.

## 1. Server PC
1. Install **Python 3.10 or newer** from python.org. On the first screen tick **"Add python.exe to PATH"**.
2. Copy the `inv-pos` folder to e.g. `C:\INV-POS` (or clone the repository there).
3. In the router, give the server PC a **fixed IP address** (DHCP reservation), e.g. `192.168.1.10`.
4. Windows: Settings → System → Power: **never sleep** when plugged in.

## 2. Install
1. Double-click **`setup.bat`**. It installs everything into a private `.venv` folder and prepares the database.
   When asked *"Load or update the City Food Court menu now?"* answer **Y**. This loads all 328 menu items
   (325 on sale, 3 hidden until priced), sets **VAT 16 % included in prices**, the business details and order types.
2. Right-click **`scripts\windows\install_tasks.bat`** → **Run as administrator**. This:
   * starts INV-POS automatically with Windows,
   * makes a verified database backup every night at 23:30 and files the end-of-day reports at 23:45,
   * opens port 8000 for the restaurant network (private networks only).
3. Start it now: `schtasks /run /tn "INV-POS Server"` (or just double-click `run.bat` for a visible window).
   Only one copy can run at a time.

## 3. First login and setup (manager/admin, about 20 minutes)
Open `http://localhost:8000` on the server.
1. Log in with PIN **1234**; choose a new admin PIN (not repeated or sequential digits).
2. **Dashboard → "Getting ready to trade"** lists what is left. Work through it:
   * **Setup → Staff**: add every server, cashier and manager with a temporary PIN. Each person chooses their own PIN at first login.
     Servers and cashiers use the *Cashier* role; the store person uses *Stock controller*.
   * **Setup → Menu → "review only"**: 10 items need a price decision (see `docs/CFC_MENU.md`). Open each, fix the price, Save.
   * **Setup → Categories**: major groups are already set (Food, Beverage, Extras, Included sides).
   * **Setup → Items & thresholds**: enter purchase costs and reorder levels for the stock items.
   * **Setup → Import / export → Import stock history**: load `oct 2026 stock movement.xlsx` if you want October's counts as the starting stock.
   * **Setup → Settings**: check receipt footer, KRA PIN (optional), serving periods.
3. **Open / Close day → Open day**: count the store (first day only) and open.

## 4. Tills and tablets
* Windows till: Chrome/Edge → `http://192.168.1.10:8000` (your server's IP) → ⋯ → **Apps → Install this site as an app**. For silent receipt printing, start Chrome with `--kiosk-printing`.
* Android tablet: Chrome → the same address → ⋮ → **Add to Home screen**. Install the printer maker's print service (or RawBT for Bluetooth ESC/POS printers) for receipts.
* Scanner: plug in, open the POS, scan into the search box.

## 5. Daily routine
| When | Who | What |
|---|---|---|
| Opening | Manager | **Open / Close day → Open day** (stock carries over from last night) |
| Start of shift | Each cashier | **Open shift** with the cash float |
| Service | Servers | **+ New check** → table number → tap items (options pop up, e.g. beef/chicken, sizes) → **+ note** for "no onions"/sides → **Send to kitchen** (prints the kitchen ticket) |
| Service | Counter | **Counter sale** for takeaway: tap items → Cash / M-Pesa / Card |
| Paying | Cashier | Tap the check → Cash (F4) / M-Pesa (F7) / Card (F8) → print receipt |
| Waste | Anyone | **Stock → Record waste** as it happens (e.g. chapati that overstayed) |
| End of shift | Each cashier | **Shift → count the drawer → Close shift** |
| Closing | Manager | All checks paid or cancelled, shifts closed → **Open / Close day → count stock → Close day** → report |
| Next morning | Manager | **Reports → Reports** (sales, cashier over/short, voids) or the end-of-day pack in `data\reports\` |

## 6. Updating INV-POS later
1. Make sure tonight's backup exists (`data\backups\`), and create an empty file `data\maintenance.flag` so the watchdog doesn't restart the server mid-update (delete it when done).
2. Replace the program files (never delete the `data` folder).
3. Run `setup.bat` again (answer N to the menu question unless the menu file changed), then restart with `scripts\windows\restart_server.bat` (as administrator).
Database upgrades run automatically and back up the database first.

## If something goes wrong
* **System frozen (the MICROS "reset")**: INV-POS resets itself. A watchdog checks the server every 2 minutes and restarts it if it doesn't answer, usually within 3 minutes; tills show "No connection to the POS server" until it's back, then carry on. To restart straight away, right-click `scripts\windows\restart_server.bat` → *Run as administrator* (put a shortcut on the desktop). Nothing is lost either way: paid sales are saved instantly, open checks stay open. Restarts are logged in `data\watchdog.log`.
* **One till frozen**: reload the page (F5 on a PC, pull down on a tablet). The cashier stays logged in and items not yet sent or paid come back into the cart.
* **Tablets still can't connect**: check the server is on and the IP hasn't changed; `ipconfig` on the server; firewall rule from step 2.
* **Forgot the admin PIN**: on the server, double-click `scripts\windows\reset_admin_pin.bat`; it prints a temporary PIN that must be changed at login.
* **Restore a backup**: stop the server, copy a file from `data\backups\` over `data\cfc.db` (delete `cfc.db-wal` and `cfc.db-shm`), start again.

Not yet tested on physical printers, scanners, or the restaurant's own PCs: do a full trial day before going live (see the pilot plan in README).
