# Asistentka – osobní asistentka zdarma (iPhone + iPad)

Co umí:
- **Kalendář** (iCloud): hlídá události na 48 h dopředu a posílá připomínky 60 a 15 minut předem.
- **Ranní souhrn** (7:00–11:00, jednou denně): dnešní události, důležité e-maily z noci, počet přeskočených reklam.
- **E-maily** (Seznam): třídí na *Důležité*, *K přečtení* a *Reklama*. Reklamu nemaže, jen ji schovává.
- **Zakázky**: seznam materiálu a objednávek pro každou zakázku, s odškrtáváním „objednáno“.
- **Cenové nabídky**: vytvoření, číslování, DPH (nebo neplátce), tisk / PDF přes sdílení.

Vše běží zdarma: GitHub Actions (veřejný repozitář), GitHub Pages, ntfy.sh (push), volitelně Gemini API.

**Bezpečnost:** e-maily a události se ukládají jen **zašifrované** (AES-256, klíč z hesla, které znáš jen ty). Repozitář může být veřejný, protože nic čitelného neobsahuje. Hesla k účtům patří **jen do GitHub Secrets**, nikdy do chatu ani do kódu.

## Nastavení krok za krokem

### 1. Účet a repozitář
1. Založ si účet na https://github.com (zdarma).
2. Vytvoř **veřejný** repozitář `asistentka` a nahraj do něj obsah této složky.
   (Veřejný repozitář = neomezené minuty pro GitHub Actions. U soukromého by běh každých 15 minut překročil zdarma limit.)
3. **Settings → Pages → Source: Deploy from a branch → Branch: `main`, folder `/ (root)`.**
   Aplikace bude na `https://TVOJE-JMENO.github.io/asistentka/`.

### 2. Tajné údaje (Settings → Secrets and variables → Actions → New repository secret)

| Název | Co tam dát |
|---|---|
| `ASIST_PASS` | Heslo pro šifrování dat (vymysli dlouhé a zapiš si ho). **Bez něj nejde nic otevřít.** |
| `MAIL_USER` | Tvoje e-mailová adresa na Seznamu |
| `MAIL_PASS` | Heslo k Seznam e-mailu (IMAP musí být zapnutý v nastavení schránky) |
| `ICLOUD_USER` | Apple ID (e-mail) |
| `ICLOUD_APP_PASS` | **Heslo pro aplikace** z https://account.apple.com (Přihlášení a zabezpečení → Hesla pro aplikace). Běžné heslo Apple ID nefunguje. |
| `NTFY_TOPIC` | Náhodný řetězec, např. `asist-8f3k2q9xL7` (ber ho jako tajné heslo) |
| `GEMINI_API_KEY` | *(volitelné)* klíč z https://aistudio.google.com/apikey. Bez něj třídí pravidla. |
| `VIP_SENDERS` | *(volitelné)* e-maily nebo jména, která jsou vždy důležitá, oddělená čárkou, např. `ucetni@firma.cz, Novák` |

### 3. Notifikace na iPhone
1. Nainstaluj z App Store aplikaci **ntfy** (zdarma).
2. V ní přidej odběr (Subscribe) tématu se stejnou hodnotou, jakou máš v `NTFY_TOPIC`.

### 4. Aplikace na ploše iPhonu i iPadu
1. Otevři v **Safari** `https://TVOJE-JMENO.github.io/asistentka/`.
2. Klepni na sdílení (čtvereček se šipkou) → **Přidat na plochu**.
3. Spusť ikonu z plochy, zadej heslo `ASIST_PASS` a zaškrtni „Pamatovat na tomto zařízení“.

### 5. První spuštění
V repozitáři otevři **Actions → Asistentka → Run workflow**. Po minutě se objeví `data.enc.json` a aplikace začne ukazovat data. Dál to běží samo každých 15 minut.

## Omezení (upřímně)
- GitHub Actions spouští úlohy přibližně každých 15 minut, takže připomínka může přijít s několikaminutovým zpožděním.
- Zakázky a nabídky se ukládají **v prohlížeči daného zařízení** (iPhone a iPad mají každý vlastní). Synchronizace mezi zařízeními je plánovaná další verze.
- AI plánování podnikání (např. „co musím objednat na zakázku X“) je v této verzi jen jako ruční seznam. AI pomocník pro plánování přijde ve verzi 2.
- Bezplatný tarif Gemini API může podle podmínek Googlu použít data k vylepšování služeb. Pokud to nechceš, nevyplňuj `GEMINI_API_KEY`. Třídění pak běží jen podle pravidel.
- Asistentka **nic neodesílá ani nezapisuje** za tebe (podle tvé volby „jen navrhovat“). Pokud se to změní, doplní se to do scriptu.

## Lokální test (bez přístupů)
```bash
pip install -r requirements.txt
ASIST_PASS=test123 python3 asistentka.py --demo
cd docs && python3 -m http.server 8000   # otevři http://localhost:8000 a zadej test123
```
