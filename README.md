# ClipperHub — instalare pe Windows

## 1. Instalează din PowerShell

Rulează linia de mai jos în PowerShell, fără a-l deschide ca administrator. Dacă `winget` lipsește, instalează sau actualizează [App Installer](https://apps.microsoft.com/detail/9nblggh4nns1) din Microsoft Store.

```powershell
$setup = Join-Path $env:TEMP 'Install-ClipperHub.ps1'; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/OctavGFX/ClipperHub/main/Install-ClipperHub.ps1' -OutFile $setup -ErrorAction Stop; powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File $setup
```

Poți și să consulți [scriptul de instalare](./Install-ClipperHub.ps1) înainte să îl rulezi. Comanda nu schimbă politica PowerShell a sistemului.

Installerul pregătește Python, FFmpeg, Deno și Firefox dacă lipsesc, verifică SHA-256 al ZIP-ului oficial și instalează ClipperHub în `%LOCALAPPDATA%\ClipperHub`. Nu suprascrie o instalare existentă. La pornirea installerului alegi:

- **0 / Enter:** fără AI local;
- **1:** Ollama + `qwen3:4b-instruct` pentru text;
- **2:** Ollama + text și `qwen3-vl:4b-instruct-q4_K_M` pentru analiză de imagine; cere mai multă memorie și spațiu.

Pentru opțiunile **1** și **2**, installerul descarcă modelele și configurează automat Ollama. ClipperHub pornește la final. Pentru următoarele porniri, deschide `Start-ClipperHub.cmd` din folderul instalării și lasă fereastra deschisă. Dacă browserul nu pornește automat, deschide [dashboardul](http://127.0.0.1:8765/).

## 2. Conectează doar serviciile pe care le folosești

- **Streamuri YouTube:** autentifică-te pe YouTube în Firefox. Sesiunea de vizionare este separată de autorizarea pentru publicare.
- **YouTube Shorts (publicare directă):** creează un proiect în [Google Cloud Console](https://console.cloud.google.com/), activează **YouTube Data API v3** și configurează **Google Auth Platform**. La **Audience → External**, dacă aplicația este în **Testing**, adaugă contul tău la **Test users**; în acest mod autorizarea expiră după 7 zile. La **Clients → Create client**, alege **Desktop app**. În ClipperHub: **Settings → Accounts & keys → YouTube Shorts**, introdu **Client ID** și **Client secret**, apoi apasă **Save → Connect YouTube**, autorizează canalul corect și apasă **Check**. Nu este suficientă o cheie API. [Proiectele fără audit YouTube pot încărca doar videoclipuri private](https://developers.google.com/youtube/v3/docs/videos/insert).
- **Telegram:** creează un bot cu [/newbot la BotFather](https://t.me/BotFather). În **Accounts & keys → Telegram bot**, salvează tokenul. În chatul privat cu botul apasă **Start**, apoi **Connect & test** în ClipperHub. Pentru prima conectare, lasă modul de livrare pe **Review in ClipperHub**.
- **Instagram Reels:** folosește un cont **Creator** sau **Business**. În [Meta for Developers](https://developers.facebook.com/apps/) creează o aplicație cu **Instagram API with Instagram Login**, adaugă contul și generează un token cu permisiuni de publicare. Rulează `winget install --id Cloudflare.cloudflared -e`, redeschide PowerShell și repornește ClipperHub. În **Accounts & keys → Instagram Reels**, apasă **Save → Check** după ce introduci tokenul.
- **Gemini (alternativă la Ollama):** creează o cheie din [Google AI Studio](https://aistudio.google.com/apikey), salveaz-o în **Accounts & keys → API keys**, apoi selectează **Google Gemini** în **AI** și apasă **Save settings**.
- **Twitch (opțional, pentru clipurile marcate de spectatori în replay):** în [Twitch Developer Console](https://dev.twitch.tv/console/apps) înregistrează o aplicație cu redirect `http://localhost`, generează un secret și salvează **Twitch Client ID** și **Twitch Client Secret** în **Accounts & keys → API keys**.
- **Upload-Post (alternativă):** creează o cheie și un profil cu conturile sociale conectate în [Upload-Post](https://www.upload-post.com/). Salvează cheia în **Accounts & keys → API keys**, apoi selectează **Use Upload-Post** pentru platforma dorită și completează profilul în **Advanced Settings → Posting**. Apasă **Save settings**.

Nu încărca fișierul `.env` sau datele aplicației pe GitHub.

**Limitare în v1.0.0:** ZIP-ul nu include modulul `ssrf_guard` necesar pentru importul manual și monitorizarea uploadurilor YouTube. Instalarea Firefox sau configurarea OAuth nu rezolvă această limitare.
