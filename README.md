# ClipperHub — instalare pe Windows

## 1. Instalează dependențele

În PowerShell, rulează comenzile de mai jos. Dacă `winget` lipsește, instalează sau actualizează [App Installer](https://apps.microsoft.com/detail/9nblggh4nns1) din Microsoft Store.

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
winget install --id DenoLand.Deno -e
winget install --id Mozilla.Firefox -e
```

FFmpeg este necesar pentru clipuri; Deno este folosit de yt-dlp pentru YouTube. Pentru streamuri YouTube, deschide Firefox și autentifică-te pe YouTube: ClipperHub citește local sesiunea Firefox, separat de autorizarea pentru publicare.

## 2. Descarcă și pornește aplicația

Deschide un PowerShell nou, ca să preia comenzile nou instalate. Folosește ZIP-ul **ClipperHub** atașat release-ului, nu arhiva „Source code”.

```powershell
$release = Invoke-RestMethod 'https://api.github.com/repos/OctavGFX/ClipperHub/releases/latest'
$asset = $release.assets | Where-Object { $_.name -like 'ClipperHub-v*.zip' } | Select-Object -First 1
if (-not $asset) { throw 'Release-ul nu are un ZIP de instalare.' }
$zip = Join-Path $env:TEMP $asset.name
$app = Join-Path $env:LOCALAPPDATA 'ClipperHub'
if (Test-Path -LiteralPath $app) { throw 'ClipperHub este deja instalat în acest folder.' }
Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $zip
Expand-Archive -LiteralPath $zip -DestinationPath $app
Set-Location $app
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

Pentru pornire, rulează blocul de mai jos. Lasă fereastra deschisă și intră în [dashboard](http://127.0.0.1:8765/) dacă browserul nu se deschide automat.

```powershell
Set-Location (Join-Path $env:LOCALAPPDATA 'ClipperHub')
$env:Path = "$(Join-Path $PWD.Path '.venv\Scripts');$env:Path"
.\ClipperHub.bat
```

## 3. Conectează Ollama (opțional, pentru AI local)

1. Rulează [installerul Ollama pentru Windows](https://ollama.com/download/windows) și pornește **Ollama** din meniul Start. Dacă serverul nu pornește, rulează `ollama serve` într-un PowerShell separat și lasă-l deschis.
2. Într-un PowerShell nou, descarcă modelul de text. Modelul de imagine este opțional și consumă memorie suplimentară.

   ```powershell
   ollama pull qwen3:4b-instruct
   ollama pull qwen3-vl:4b-instruct-q4_K_M
   ollama list
   Invoke-RestMethod 'http://127.0.0.1:11434/api/tags'
   ```

3. În **Settings → AI**, selectează **Local AI (Ollama)**. În **Advanced Settings → AI review and copy**, setează URL-ul `http://127.0.0.1:11434/v1`, modelul `qwen3:4b-instruct` și, dacă l-ai descărcat, modelul de imagine `qwen3-vl:4b-instruct-q4_K_M`. Apasă **Save settings**. Ollama local nu cere cheie API.

## 4. Conectează doar serviciile pe care le folosești

- **YouTube Shorts (publicare directă):** creează un proiect în [Google Cloud Console](https://console.cloud.google.com/), activează **YouTube Data API v3** și configurează **Google Auth Platform**. La **Audience → External**, dacă aplicația este în **Testing**, adaugă contul tău la **Test users**; în acest mod autorizarea expiră după 7 zile. La **Clients → Create client**, alege **Desktop app**. În ClipperHub: **Settings → Accounts & keys → YouTube Shorts**, introdu **Client ID** și **Client secret**, apoi apasă **Save → Connect YouTube**, autorizează canalul corect și apasă **Check**. Nu este suficientă o cheie API. [Proiectele fără audit YouTube pot încărca doar videoclipuri private](https://developers.google.com/youtube/v3/docs/videos/insert).
- **Telegram:** creează un bot cu [/newbot la BotFather](https://t.me/BotFather). În **Accounts & keys → Telegram bot**, salvează tokenul. În chatul privat cu botul apasă **Start**, apoi **Connect & test** în ClipperHub. Pentru prima conectare, lasă modul de livrare pe **Review in ClipperHub**.
- **Instagram Reels:** folosește un cont **Creator** sau **Business**. În [Meta for Developers](https://developers.facebook.com/apps/) creează o aplicație cu **Instagram API with Instagram Login**, adaugă contul și generează un token cu permisiuni de publicare. Rulează `winget install --id Cloudflare.cloudflared -e`, redeschide PowerShell și repornește ClipperHub. În **Accounts & keys → Instagram Reels**, apasă **Save → Check** după ce introduci tokenul.
- **Gemini (alternativă la Ollama):** creează o cheie din [Google AI Studio](https://aistudio.google.com/apikey), salveaz-o în **Accounts & keys → API keys**, apoi selectează **Google Gemini** în **AI** și apasă **Save settings**.
- **Twitch (opțional, pentru clipurile marcate de spectatori în replay):** în [Twitch Developer Console](https://dev.twitch.tv/console/apps) înregistrează o aplicație cu redirect `http://localhost`, generează un secret și salvează **Twitch Client ID** și **Twitch Client Secret** în **Accounts & keys → API keys**.
- **Upload-Post (alternativă):** creează o cheie și un profil cu conturile sociale conectate în [Upload-Post](https://www.upload-post.com/). Salvează cheia în **Accounts & keys → API keys**, apoi selectează **Use Upload-Post** pentru platforma dorită și completează profilul în **Advanced Settings → Posting**. Apasă **Save settings**.

Nu încărca fișierul `.env` sau datele aplicației pe GitHub.

**Limitare în v1.0.0:** ZIP-ul nu include modulul `ssrf_guard` necesar pentru importul manual și monitorizarea uploadurilor YouTube. Instalarea Firefox sau configurarea OAuth nu rezolvă această limitare.
