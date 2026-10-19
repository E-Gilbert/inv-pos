# Deployment: Windows server, Windows tills, Android tablets

## Architecture decision

INV-POS stays a **LAN web application** served by one Windows (or Linux) PC, opened in a browser on every device. No rewrite, no Electron/native wrapper:

| Need | How it is met |
|---|---|
| Windows tills | Chrome/Edge in full-screen (F11) or "Install app" (Edge/Chrome → ⋯ → Apps). Keyboard shortcuts: F2 scan/search, F4 cash, F7 M-Pesa, F8 card, Esc close |
| Android tablets/phones | Chrome → ⋮ → *Add to Home screen*. Touch targets ≥ 46 px on touch screens, responsive layout, "View order" bar on phones |
| One source of truth | All tills write to one SQLite database on the server inside locked transactions — no sync conflicts are possible |
| Updates | Replace the files on the server and restart `run.bat`; every device gets the new version on reload. Database migrations run automatically, with a backup first |

A desktop wrapper would only add value for direct USB receipt printing or a cash-drawer kick; that can be added later as a small local print helper without changing this app.

## Server install (Windows)

1. Install Python 3.10+ from python.org (tick **Add python.exe to PATH**).
2. Copy the project folder (e.g. `C:\CFC\inv-pos`). Double-click `setup.bat` once.
3. Double-click `run.bat`. Allow Python through Windows Firewall on **Private** networks when asked.
4. Give the server a **fixed IP** (router DHCP reservation), plug it into a **UPS**, disable sleep.
5. Start automatically: Task Scheduler → *At log on* → run `run.bat`.
6. Nightly backup: Task Scheduler → daily 23:30 → `.venv\Scripts\python.exe scripts\backup.py`.
7. First login with PIN 1234 → you must choose a new admin PIN.

**Updating:** stop `run.bat`, run `python scripts\backup.py`, replace the code (keep the `data\` folder!), run `setup.bat` (installs new dependencies), start `run.bat`.

Run a single server process only (the default). The login throttle lives in memory.

## Connectivity behaviour (no offline-sales claim)

* **Internet down:** nothing changes. Everything runs on the LAN.
* **LAN/server down:** devices show a red banner *"No connection to the POS server — sales are paused"*. Sales cannot be taken until it returns. Use a paper docket and enter the sales afterwards.
* **Connection lost mid-payment:** the till keeps the cart and shows *Retry payment*. The retry reuses the same checkout key, so if the first attempt reached the server you get the original receipt ("Already paid — no second charge") and stock is not deducted twice.

A durable offline queue (selling without the server) is **not implemented**. It would need local receipt numbering, deferred stock deduction and conflict handling. Don't promise it to the client until it is built and tested.

## Barcode scanners

Any USB or Bluetooth scanner in **keyboard (HID) mode** with an *Enter* suffix works: scan into the POS search box (it is focused automatically on Windows tills; tap it first on tablets). A unique match is added to the cart. Barcodes and SKUs are set per menu item under Setup → Menu, and are unique across menu items and stock items.
Tested: keyboard-wedge input in Chromium. **Not tested on physical scanner hardware.**

## Receipt printing

The receipt is laid out for **80 mm** thermal paper and printed through the browser print dialog.
* **Windows:** install the printer's Windows driver, set it as default, set paper to 80 mm and margins to none. For one-click printing, start Chrome with `--kiosk-printing`.
* **Android:** install the printer vendor's print-service app (e.g. Epson iPrint, Star, or RawBT for generic ESC/POS Bluetooth printers) and enable it under *Settings → Connected devices → Printing*.
**Not tested on physical printers.** Direct ESC/POS printing and a cash drawer kick are not implemented.

## HTTPS (optional)

Over plain HTTP on a private LAN, PIN logins are visible to anyone sniffing that Wi-Fi. Use WPA2/WPA3 with a strong password and keep guests on a separate network. For HTTPS, create a certificate for the server's IP (e.g. with `mkcert`), install the root certificate on each device, and run:
`python -m uvicorn app.main:app --host 0.0.0.0 --port 8443 --ssl-keyfile key.pem --ssl-certfile cert.pem`

## Licensing (vendor notes)

* Default `CFC_LICENCE_MODE=development`: status is shown, nothing is ever blocked.
* Production (only after the owner approves): run `python scripts/licence_tool.py genkey` on the vendor's own machine, keep the private key secret, put the public key in `data/licence_public.key` on the server, issue a licence with `licence_tool.py issue …`, install it under Setup → Licence & devices, then set `CFC_LICENCE_MODE=enforce` in `run.bat`.
* Enforcement only blocks new sales, receipts and day openings. Login, reports, exports and closing the day/shifts always work. Verification is offline; there is no licence server to fail.
