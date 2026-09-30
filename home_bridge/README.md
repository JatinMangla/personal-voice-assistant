# Jarvis home helper: your Samsung washing machine, free

Samsung's SmartThings API costs money from October 2026. But your washer (WW12DG6B24ASTL,
a 2024 "DG" model) also answers **directly on your home Wi-Fi**, the same way the SmartThings
app talks to it locally. This helper runs on your **Android phone** at home, talks to the
washer over Wi-Fi, and passes messages to Jarvis. Cost: ₹0.

What you can then say to Jarvis:
- "Is the washing machine done?" / "How long is left on the wash?"
- "Start the washing machine" / "Pause the wash" / "Stop the washing machine": Jarvis asks
  "Should I …?" first and acts only after your "yes". The washer itself also only accepts it
  while its **Smart Control** button is on (Samsung's safety rule).
- Bonus: your phone gets a notification **"The washing machine has finished."** when a wash
  ends, even when Jarvis is off.

It works while your phone is at home on the same Wi-Fi as the washer.

**Honest note:** this uses community software (SmartThings-Local, MIT licence) that works out
Samsung's local protocol. It is not official. Samsung could change it in a firmware update.
Nothing here can harm the washer; the worst case is that it stops answering.

---

## One-time setup (about 30 minutes)

### 1. Install Termux on the phone
The Play Store version is old. Get it from F-Droid:
1. On the phone, open **f-droid.org** in Chrome → **Download F-Droid** → install it (allow
   "install unknown apps" for Chrome when Android asks).
2. In F-Droid, search **Termux** → Install. Optional, for the "finished" notification: also
   install **Termux:API**.

### 2. Install the software (inside Termux)
Open Termux and type these lines one by one (press Enter after each; answer `y` if asked):
```
pkg update -y && pkg upgrade -y
pkg install -y python git openssl-tool python-cryptography termux-api
pip install smartthings-local cbor2 websockets requests
termux-setup-storage
```
The last line asks for storage permission: allow it.
If the `pip install` line fails with a message about "Rust" or "cryptography", type
`pkg install -y rust` and run the `pip install` line again.

### 3. Get the two files from GitHub onto the phone
1. On the phone, in Chrome (signed in to GitHub), open your repo
   **JatinMangla/personal-voice-assistant** → folder `home_bridge` → `samsung_bridge.py` →
   the **⋯** menu → **Download** (or "Raw", then save).
2. Same for `scripts/find_samsung.py`.
3. In Termux:
   ```
   mkdir -p ~/jarvis/certs
   cp ~/storage/downloads/samsung_bridge.py ~/storage/downloads/find_samsung.py ~/jarvis/
   cd ~/jarvis
   ```

### 4. Find the washer on the Wi-Fi
Switch the washer **on** (it leaves Wi-Fi when off), then in Termux:
```
python find_samsung.py
```
It prints lines like `192.168.1.50: secure DTLS service on UDP 5684 -> METHOD 2`. Note that
address. (Tip: in your router's settings, "reserve" this address for the washer so it never
changes.) Nothing found? Send the output to Claude.

### 5. Make the washer's local key (certificate)
```
cd ~
git clone https://github.com/QuiteYellow/SmartThings-Local
cd SmartThings-Local
pip install -r requirements-bootstrap.txt
TARGET_IP=192.168.1.50 python setup_cert.py --test
```
(use the address from step 4). The end of the output says whether the washer accepted it.
- **Accepted:** copy the key files:
  `cp certs/client_fullchain.pem certs/client.key ~/jarvis/certs/`
- **Rejected** (e.g. `unknown_ca`): try `TARGET_IP=192.168.1.50 python setup_cert.py --fallback --test`.
  Still rejected? Your washer is one of the newest "OCF-PKI" machines, which the community tool
  can't do yet. Send the output to Claude.

### 6. The helper's password (BRIDGE_KEY)
1. On the laptop, in the project folder: `.venv\Scripts\python scripts\make_key.py` and copy
   the line it prints.
2. Add it as a secret named **BRIDGE_KEY** in Kaggle (Add-ons → Secrets, tick it for the Jarvis
   notebook) **and** on GitHub (repo → Settings → Secrets and variables → Actions → New
   repository secret).
3. On the phone, in Termux, create the settings file, one line at a time (put your key and
   the washer's address in place of the examples):
   ```
   cd ~/jarvis
   echo "JARVIS_URL=https://volley-gnarly-constable.ngrok-free.dev" > bridge.env
   echo "BRIDGE_KEY=paste-the-key-here" >> bridge.env
   echo "WASHER_IP=192.168.1.50" >> bridge.env
   ```

`JARVIS_URL` is your usual ngrok address while Jarvis runs on Kaggle (the helper's own
path is protected by BRIDGE_KEY instead of Google sign-in). Later, when Jarvis runs on a home
PC with Tailscale, put the PC's `https://….ts.net` address here instead.

### 7. Start the helper
```
termux-wake-lock
cd ~/jarvis && python samsung_bridge.py
```
It prints the washer's status, then waits for Jarvis. Leave Termux running in the background.
So that Android doesn't stop it: phone **Settings → Apps → Termux → Battery → Unrestricted**
(the name differs a little per phone).

Every time Jarvis starts, the helper connects by itself within a few seconds. After a phone
restart, open Termux and run the two lines of step 7 again.

(Home PC with Tailscale only:) if Termux says it can't find the `ts.net` name, tell Termux
to use Tailscale's name server:
```
echo "nameserver 100.100.100.100" > $PREFIX/etc/resolv.conf
echo "nameserver 8.8.8.8" >> $PREFIX/etc/resolv.conf
```

## Checking it works
1. Start Jarvis as usual (Start button).
2. The Kaggle log shows `[bridge] home helper connected (1 device(s))`.
3. Ask: "Is the washing machine running?"

## Privacy and safety
- The washer's key files and `bridge.env` stay on your phone. Don't share them.
- Jarvis only ever sends: status, start, pause or stop. Anything else is refused.
- The helper's connection to Jarvis is encrypted (https) and needs BRIDGE_KEY; it is the only
  path that doesn't use Google sign-in, because a phone script can't do a Google login.
- The "Jarvis is up" wake-up goes through ntfy.sh with the content "up" only, on a topic
  name derived from BRIDGE_KEY that nobody can guess.
