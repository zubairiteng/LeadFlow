# LeadFlow — AI Lead Generation + Email Outreach (Flask, 100% free)

Ek hi software me: **customers dhoondna (Discovery Bot)**, **subscribers/customer list**, **email sending + replies reading**,
**rounds + follow-ups + analytics**. Aap ke "AI Lead Gen" document ke Part A–H ke mutabiq banaya gaya hai (ERPNext ki jagah
Python/Flask), Mautic jaisa idea: subscriber list aur email settings ek jagah.

Koi paid cheez nahi: Flask + SQLite + free libraries. Hosting aap apne PC par ya kisi free VM par kar sakte hain.

---

## 1. Run kaise karein (5 minute)

Zaroorat: **Python 3.10 ya naya** (python.org se install karein; Windows par "Add Python to PATH" tick karein).

### Windows (PowerShell)
```powershell
cd LeadFlow                      # zip extract wala folder
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

### Mac / Linux
```bash
cd LeadFlow
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python run.py
```

Phir browser me kholein: **http://localhost:5000** → **Create an account** (pehla account us company ka Admin hota hai).

> Pehli baar demo dekhna ho to: Settings → **Sending rules** me postal address bharein, Settings → **Email** me **Use demo mode** dabayein.
> Demo mode me asli email nahi jati, lekin poora flow chalta hai (approval, sending, follow-ups, reply/bounce simulate).
> Client ko dikhane ke liye yeh sab se behtar hai.

---

## 2. Pehle test chalayein (optional lekin recommended)

```bash
python -m pytest tests -q        # 72 tests, ~5 second
```

---

## 3. Client ke liye setup (doc ke mutabiq sirf 2 kaam)

| Kaam | Kahan | Kya karna hai |
|---|---|---|
| **1. AI key** (optional) | Settings → AI key | Provider chunein (Gemini / Claude / OpenAI), key paste karein, **Save and test**. Key encrypted store hoti hai aur masked dikhti hai. Gemini ki free key: aistudio.google.com/apikey |
| **2. Email connect** | Settings → Email | **Option B (asaan, recommended)**: Gmail App Password. **Option A**: "Connect with Gmail" (Google OAuth, neeche dekhein). |

AI sirf email ka draft likhta hai. **Score AI nahi, fixed rules deti hain** (Hot ≥ 75, Warm 50–74, Cold < 50).

### Option B — Gmail App Password (Google approval ki zaroorat nahi)
1. Naye (alag) Gmail account me **2-Step Verification** on karein.
2. https://myaccount.google.com/apppasswords se App Password banayein.
3. LeadFlow → Settings → Email → Option B: sender email, host `smtp.gmail.com`, port `587`, IMAP `imap.gmail.com` `993`, password = App Password → **Test and save**.
4. Reply/bounce/unsubscribe is IMAP se parhe jate hain (har 10 minute).

### Option A — "Connect with Gmail" (developer ek dafa setup karta hai)
1. console.cloud.google.com → naya project → **Gmail API** enable.
2. OAuth consent screen + **OAuth client (Web application)** banayein. Redirect URI: `http://YOUR-ADDRESS/oauth/google/callback`
   (exact URL Settings → Email page par dikhta hai).
3. `.env` me `GOOGLE_CLIENT_ID` aur `GOOGLE_CLIENT_SECRET` daalein, `BASE_URL` set karein, app restart karein.
4. **Dhyan:** jab tak Google app ko approve na kare, user ko "unverified app" warning milti hai aur har ~7 din baad reconnect karna parta hai
   (doc Part G). Isi liye Option B pehle recommend hai.

---

## 4. Rozana ka workflow (doc Part A)

1. **Campaigns → New campaign** (naam, product, target market).
2. **Find customers**: keyword, country, city, company type, lead target → *Queue search*. Bot har 10 minute me sab se purani queued job uthata hai (ya **Run now**).
   Duplicate domains aur Excluded list wale skip hote hain. Agar sources khatam ho jayen to job **"Completed with Shortfall"** hoti hai — fake leads kabhi nahi banti.
3. **Emails**: website se (Home/Contact/About pages) business emails khud dhoondi jati hain (Gmail/Yahoo/Hotmail drop). Har email *Unverified / Risky / Invalid* label hoti hai.
4. **Customer list review**: filter (grade, country, email status), **Exclude** (permanent; Excluded list se undo), manual add, **CSV/Excel upload**.
5. **New round** (Manager): audience chunein, email likhein (ya AI draft), follow-up days (default 3 aur 7).
6. **Approval gate**: final count dekhein → **Send test email** → **Approve and start**. Approve ke baad text frozen hota hai. Bina Approve ke kuch nahi jata.
7. **Sending** khud hoti hai: working-hour window, 1–3 minute random gap, daily limit (default 50, max 100, pehle hafte 20), har email se pehle do-not-contact dobara check, har email me unsubscribe link + postal address.
8. **Replies**: Replied / Auto-reply (follow-up nahi rukta) / Bounced / Unsubscribed khud sort hote hain. Reply par follow-ups band aur **Needs reply** me task ban jata hai.
9. **Round 2**: *New round* → "Ignored customers" ya "Not contacted yet" ya hand-pick. Replied/Unsubscribed/Bounced/Excluded kabhi shamil nahi hote. 14 din se kam gap par warning.
10. **Analytics**: campaign page par sab rounds side-by-side (Selected, Sent, Bounced, Replied, Ignored, Unsubscribed, Reply rate).

**Roles:** Admin (settings, users) · Manager (rounds approve) · Researcher (search + customer list, approve/send nahi kar sakta).

### Apni website se subscribers (Mautic jaisa)
Campaign page → *Website sign-up form* me ek link/iframe milta hai. Website par lagayein — consent tick karne wale log us campaign ki customer list me aa jate hain (spam-honeypot + rate limit ke saath).

---

## 5. Settings (environment / `.env`)

`.env.example` ko `.env` naam se copy karein:

| Variable | Matlab |
|---|---|
| `BASE_URL` | Public address (unsubscribe links isi se bante hain). Internet par chalana ho to apna domain/IP, e.g. `https://leads.example.com` |
| `PORT` | Default 5000 |
| `ALLOW_SIGNUP` | `0` karein jab apne accounts ban jayen (warna koi bhi signup kar sakta hai) |
| `POSTAL_ADDRESS` | Naye accounts ka default address |
| `GOOGLE_CLIENT_ID/SECRET` | Option A ke liye |
| `FETCH_DELAY` | Ek hi website par requests ke beech seconds (default 3) |
| `LEADGEN_INSTANCE` | Database/secret ka folder (default `instance/`) |

**Backup:** `instance/` folder copy kar lein (`leadgen.db` + `secret.key`). **`secret.key` kho gayi to saved AI/SMTP passwords parhe nahi ja sakte.**

---

## 6. Client ko link dena (hosting)

* **Bilkul free:** apna PC/laptop (chalta rehna chahiye) + Cloudflare Tunnel / ngrok free plan se link. Ya **Oracle Cloud "Always Free"** VM (permanent free).
* Zaroori: app ka process **hamesha chalta rahe**, kyunki sending/replies background workers karte hain. Jo free hostings "sleep" ho jati hain (e.g. free web dynos) un par sending ruk jayegi.
* Production me `pip install waitress` (requirements me hai) — `run.py` khud use kar leta hai. Sirf **ek** process chalayein.
* Docker: `docker build -t leadflow . && docker run -p 5000:5000 -v leadflow-data:/data leadflow`
* Internet par HTTPS lagayein (Caddy/nginx/Cloudflare) aur `BASE_URL` `https://…` rakhein.

---

## 7. Imaandari se limits (doc Part G ke sath)

* **Discovery sources:** OpenStreetMap (free, API key nahi), Yellow Pages (US) aur web-search+website-check. Directory sites pages badalti/block karti rehti hain aur robots.txt follow hoti hai, is liye nateeje mulk/keyword ke hisab se alag honge — har connector ko maintenance chahiye. Agar sources se kam leads milen to system sach batata hai (Shortfall). **Google Search/Maps scraping jaan-boojh kar shamil nahi** (Google ki terms ke khilaf). Backup raaste: manual add, CSV/Excel upload, website form.
* **Emails:** paid checker ke bina email "Verified" nahi hoti (Unverified). Kai companies ki website par email hoti hi nahi.
* **Gmail risk:** cold email normal Gmail se spam flag ho sakti hai. Alag naya Gmail use karein, volume kam rakhein, bounces barhein to ruk jayein. Sender `@gmail.com` hoga (baad me domain/SMTP lagaya ja sakta hai — Option B me kisi bhi SMTP se kaam karta hai).
* **Legal:** har email me unsubscribe link + postal address lazmi hai; software iske baghair approve/send nahi karne deta. Apne mulk ke email-marketing qawaneen khud check karein.
* **Tracking pixels nahi**, LinkedIn scraping nahi, WhatsApp automation nahi.

## 8. Test kis cheez ka hua hai
72 automated tests: scoring, dedup, excluded list, CSV/Excel, email finder, discovery (resume, shortfall, cancel, blocked connector),
rounds/audience, approval gate, daily limit/warm-up/window/gap, follow-ups, reply/auto-reply/bounce/unsubscribe, SMTP (fake server),
IMAP + Gmail API + AI providers (mocked), security (CSRF, roles, tenant isolation, SSRF), aur poora web flow.
Asli live Gmail/Google/directories ka test is environment se mumkin nahi tha — **live jane se pehle 3–5 apne personal inboxes par staging test zaroor karein** (doc Part H step 7).

## 9. Folder
```
run.py            # start
app/              # code (services/ = business logic, views/ = pages, templates/)
tests/            # pytest
instance/         # database + secret key (auto-banta hai)
```
