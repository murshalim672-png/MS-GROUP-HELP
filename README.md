# 🤖 GroupHelp Bot (Rose-style + Wrong Format Guard)

## ⭐ मेन फीचर – Wrong Format Guard
कोई भी यूज़र ग्रुप में 7–12 digit का नंबर/UID भेजे → बॉट उसे डिलीट करेगा और आपका सेट किया हुआ **Wrong Format** मैसेज + सारे commands की लिस्ट भेजेगा। नॉर्मल चैट (hi, hello) पर कुछ नहीं होगा।
यह **डिफ़ॉल्ट में OFF** है, आप चालू करोगे तब ही चलेगा।

ग्रुप में (एडमिन बनकर) ये सेट करो:
```
/setformat on
/setwrong ❌ Wrong Format! सिर्फ ये commands use करो:
/setcmds /like region uid - free likes
/info uid - player information
```
(`/setcmds` की हर लाइन = एक command; बाद में `/addcmd`, `/delcmd 2`, `/clearcmds`, `/cmds` भी हैं)

| Command | काम |
|---|---|
| `/setformat on/off` | गार्ड चालू/बंद |
| `/setwrong <text>` | Wrong Format मैसेज (placeholders: `{mention} {first} {id} {chat} {commands}`) |
| `/setrange 7 12` | कितने digit का नंबर पकड़ना है |
| `/formatmode pure/contains` | pure = सिर्फ नंबर वाला मैसेज, contains = मैसेज में कहीं भी नंबर |
| `/formatadmin on/off` | एडमिन को छूट (टेस्ट करने के लिए off कर दो) |
| `/formatdelete on/off` | यूज़र का नंबर वाला मैसेज डिलीट हो या नहीं |
| `/formatautodel 15` | Wrong Format मैसेज कितने सेकंड बाद हट जाए (0 = ना हटे) |
| `/formatstatus` | सारी सेटिंग देखो |

`1234 5678`, `123-456-7890`, हिंदी अंक (१२३४५६७) भी पकड़ में आते हैं।

## 🚀 सेटअप (एक बार)
1. **@BotFather** → `/newbot` → टोकन लो।
2. BotFather → `/mybots` → आपका बॉट → Bot Settings → **Group Privacy → Turn OFF** (ज़रूरी!)।
3. बॉट को ग्रुप में डालो और **Admin** बनाओ (Delete messages, Ban users, Pin messages)।
4. अपना यूज़र ID @userinfobot से लो → `OWNER_ID`.

## ☁️ Render (फ्री) पर चलाना
Render का फ्री डिस्क रीस्टार्ट पर मिट जाता है, इसलिए **MongoDB Atlas (फ्री)** लगाओ:
1. mongodb.com/atlas → फ्री M0 cluster → Database Access में user+password बनाओ → Network Access में `0.0.0.0/0` allow करो → Connect → Drivers → connection string कॉपी करो (`mongodb+srv://...`).
2. इस पूरे फोल्डर को **GitHub repo** में अपलोड करो।
3. Render → New → **Web Service** → repo चुनो।
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
   - Environment Variables: `BOT_TOKEN`, `OWNER_ID`, `MONGO_URI`
4. Deploy होने के बाद Render का URL (`https://xxxx.onrender.com`) **UptimeRobot** में HTTP monitor (5 मिनट) पर लगा दो, ताकि सर्विस सोए नहीं।

## 📱 Termux पर चलाना
```
pkg update && pkg install python git -y
cd grouphelp
pip install -r requirements.txt
export BOT_TOKEN="टोकन" OWNER_ID="आपकी_id"
python main.py
```
MONGO_URI ना दो तो डेटा `data.json` में सेव होगा।

## 📋 बाकी commands
`/help` में पूरी लिस्ट है: ban/kick/mute/warn, welcome/goodbye, rules, notes (`#name`), filters, blacklist, locks, antiflood, purge, pin वगैरह।
ध्यान: बॉट अपने `/help` में `/like` या `/info` जैसे commands नहीं रखता, ये आपकी अपनी लिस्ट (`/setcmds`) के लिए हैं।
