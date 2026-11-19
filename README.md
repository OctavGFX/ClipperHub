# ClipperHub — instalare pe Windows

## 1. Instalează din PowerShell

Rulează linia de mai jos în PowerShell, fără a-l deschide ca administrator. Dacă `winget` lipsește, instalează sau actualizează [App Installer](https://apps.microsoft.com/detail/9nblggh4nns1) din Microsoft Store.

```powershell
$setup = Join-Path $env:TEMP 'Install-ClipperHub.ps1'; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/OctavGFX/ClipperHub/main/Install-ClipperHub.ps1' -OutFile $setup -ErrorAction Stop; powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File $setup
```

Poți și să consulți [scriptul de instalare](./Install-ClipperHub.ps1) înainte să îl rulezi. Comanda nu schimbă politica PowerShell a sistemului.

Installerul corectat verifică SHA-256 și fișierele obligatorii din ZIP înainte de instalare. Pregătește Python 3.12, FFmpeg/FFprobe cu suport pentru subtitrări, Deno și runtime-ul Visual C++ x64 necesar Whisper, doar dacă lipsesc. Instalează dependențele Python într-un mediu izolat, în `%LOCALAPPDATA%\ClipperHub`. Nu instalează Firefox sau alt browser și nu suprascrie o instalare existentă. La pornirea installerului alegi:

- **0 / Enter:** fără AI local;
- **1:** Ollama + `qwen3:4b-instruct` pentru text;
- **2:** Ollama + text și `qwen3-vl:4b-instruct-q4_K_M` pentru analiză de imagine; cere mai multă memorie și spațiu.

Pentru opțiunile **1** și **2**, installerul descarcă modelele și configurează automat Ollama. Separat, alegi dacă folosești publicare directă Instagram: numai atunci instalează `cloudflared`. Nu este necesar pentru YouTube, Telegram sau Upload-Post. Modelul Whisper selectat în aplicație se descarcă la prima utilizare; nu sunt descărcate toate modelele disponibile.

ClipperHub pornește la final. Pentru următoarele porniri, deschide `Start-ClipperHub.cmd` din folderul instalării și lasă fereastra deschisă. Dacă browserul nu pornește automat, deschide [dashboardul](http://127.0.0.1:8765/) în browserul tău existent.

## 2. Conectează doar serviciile pe care le folosești

- **Streamuri YouTube:** streamurile publice nu necesită un browser sau un profil Firefox. Unele surse pot cere autentificare; aceasta este separată de autorizarea pentru publicare. Pentru live-uri cu acces restricționat, adaptorul acceptă configurația de cookie-uri yt-dlp aleasă explicit de utilizator.
- **YouTube Shorts (publicare directă):** creează un proiect în [Google Cloud Console](https://console.cloud.google.com/), activează **YouTube Data API v3** și configurează **Google Auth Platform**. La **Audience → External**, dacă aplicația este în **Testing**, adaugă contul tău la **Test users**; în acest mod autorizarea expiră după 7 zile. La **Clients → Create client**, alege **Desktop app**. În ClipperHub: **Settings → Accounts & keys → YouTube Shorts**, introdu **Client ID** și **Client secret**, apoi apasă **Save → Connect YouTube**, autorizează canalul corect și apasă **Check**. Nu este suficientă o cheie API. [Proiectele fără audit YouTube pot încărca doar videoclipuri private](https://developers.google.com/youtube/v3/docs/videos/insert).
- **Telegram:** creează un bot cu [/newbot la BotFather](https://t.me/BotFather). În **Accounts & keys → Telegram bot**, salvează tokenul. În chatul privat cu botul apasă **Start**, apoi **Connect & test** în ClipperHub. Pentru prima conectare, lasă modul de livrare pe **Review in ClipperHub**.
- **Instagram Reels:** folosește un cont **Creator** sau **Business**. În [Meta for Developers](https://developers.facebook.com/apps/) creează o aplicație cu **Instagram API with Instagram Login**, adaugă contul și generează un token cu permisiuni de publicare. Alege Instagram direct în installer pentru instalarea `cloudflared`. În **Accounts & keys → Instagram Reels**, apasă **Save → Check** după ce introduci tokenul.
- **Gemini (alternativă la Ollama):** creează o cheie din [Google AI Studio](https://aistudio.google.com/apikey), salveaz-o în **Accounts & keys → API keys**, apoi selectează **Google Gemini** în **AI** și apasă **Save settings**.
- **Twitch (opțional, pentru clipurile marcate de spectatori în replay):** în [Twitch Developer Console](https://dev.twitch.tv/console/apps) înregistrează o aplicație cu redirect `http://localhost`, generează un secret și salvează **Twitch Client ID** și **Twitch Client Secret** în **Accounts & keys → API keys**.
- **Upload-Post (alternativă):** creează o cheie și un profil cu conturile sociale conectate în [Upload-Post](https://www.upload-post.com/). Salvează cheia în **Accounts & keys → API keys**, apoi selectează **Use Upload-Post** pentru platforma dorită și completează profilul în **Advanced Settings → Posting**. Apasă **Save settings**.

Nu încărca fișierul `.env` sau datele aplicației pe GitHub.

## 3. Upgrade de la v1.0.x

Release-ul **v1.1.0** include `ssrf_guard.py` și `security_utils.py`, necesare pentru importul manual și monitorizarea uploadurilor YouTube. Nu necesită Firefox. AI Director este inclus, iar studioul Narative a fost eliminat fără ștergerea proiectelor sau clipurilor existente.

Updaterul v1.0.x nu acceptă lista extinsă de fișiere, deci oferă pagina release-ului pentru un upgrade manual:

1. Oprește monitorizarea, așteaptă terminarea procesărilor și închide aplicația.
2. Descarcă `ClipperHub-Windows-v1.1.0.zip` din [pagina release-ului](https://github.com/OctavGFX/ClipperHub/releases/tag/v1.1.0).
3. Extrage conținutul peste folderul instalării și acceptă înlocuirea fișierelor aplicației. Nu șterge folderul instalării și nu rula installerul de instalare nouă peste el.
4. Păstrează `.env`, `livewatch.toml`, folderul de date, backupurile, `.venv` și programările. ZIP-ul nu conține și nu înlocuiește aceste date.
5. Pornește din nou `Start-ClipperHub.cmd` sau `ClipperHub.bat`.

Actualizările automate rămân disponibile pentru instalările v1.1.0 și ulterioare.
