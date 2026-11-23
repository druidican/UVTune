# UVTune

An undervolt tester for AMD GPUs with ROCm on Linux. It finds the lowest stable voltage offset and power limit for your card, using your own ComfyUI workflow as the load and error detector, and survives crashes and reboots during the test.

*The documentation below is in Danish.*

---

Finder automatisk den laveste stabile spænding (undervolt) til et AMD-grafikkort
på Linux, med din egen ComfyUI som belastning og fejldetektor.

Mål: **mindre strømforbrug**, **uændret eller bedre ydelse**, **stabilitet og mindre støj**.

## Sådan virker det

1. **Reference og opvarmning (5 min):** kører dit workflow på standardindstillinger, måler
   ydelse, effekt, temperatur og blæser, og gemmer referencebilledet. Kortet er varmt bagefter.
2. **Screening (3 min pr. trin):** sænker spændingen 25 mV ad gangen, indtil noget fejler,
   og går derefter 5 mV ad gangen mod grænsen. Grove fejl viser sig hurtigt.
3. **Bekræftelse (15 min):** starter med den mest moddede indstilling, der klarede
   screeningen. Fejler den, går den 5 mV op og prøver igen.
4. **Verifikation (30 min):** bekræftet indstilling + 10 mV sikkerhedsmargin.
5. **Næste power limit** (standard 100, 90, 85 og 80 %) starter screeningen ved det forriges
   bekræftede offset i stedet for ved 0 mV.
6. **Efter hver genstart** varmes kortet op i 5 min på standard spænding, før testen fortsætter.
7. **Resultat:** tre valg blandt de stabile indstillinger, med sammenligningsgrafer mod standard:
   1. *Mest undervoltet, stabil:* laveste spænding, der bestod verifikationen.
   2. *Mindst negativ effekt:* laveste strømforbrug, hvor ydelsen højst er 2 % under standard.
   3. *Bedst positiv effekt:* højeste ydelse.

Fanen **GPU (amd-smi)** viser kortinformation og live-målinger fra `amd-smi`, som følger med
ROCm. Mangler det, installeres det med `sudo apt install amd-smi-lib`.

En fejl kan være:

- **Forkert billede:** billedet afviger fra referencen. GPU'en har regnet forkert uden at crashe.
- **GPU-reset:** amdgpu melder ring-timeout eller reset i kernel-loggen.
- **ComfyUI-fejl / hang:** fx HIP-fejl, eller billedet bliver aldrig færdigt.
- **Frys:** maskinen frøs eller genstartede. Det opdages ved næste boot.

Efter en GPU-fejl genstarter maskinen (kan slås fra), og testen fortsætter af sig selv
med næste trin. Ved genstart nulstiller kernen altid kortet til standard.

## Før du installerer

1. **Overdrive skal være slået til.** I LACT: *Enable overclocking*, derefter genstart.
   Det svarer til `amdgpu.ppfeaturemask=0xffffffff`.
2. **Workflow:** lav et workflow i ComfyUI med fast seed ("control after generate" =
   *fixed*). Hvert billede skal helst tage 20–60 sekunder. Gem det med **Export (API)**.

## Installation

```bash
tar xzf uvtune.tar.gz
cd uvtune
sudo ./install.sh
```

Start derefter **UV Tune** fra menuen eller med `uvtune-gui`. Appen åbner i browseren
på http://127.0.0.1:8090.

## Rapport og nedlukning

- **Rapport:** når testen slutter, gemmes en rapport i den mappe, du vælger under Opsætning
  (standard `~/Dokumenter/UV Tune`). Den består af en HTML-fil, der kan åbnes i enhver
  browser uden internet, og en CSV-fil med alle målinger til fx LibreOffice Calc. Under
  **Resultater** kan du også gemme eller downloade rapporten for en tidligere kørsel.
- **Sluk når færdig:** rapporten gemmes først. Derefter er der 60 sekunders nedtælling, og
  computeren slukker. Nedtællingen kan annulleres med knappen *Annullér nedlukning* i appen på
  Odin. Computeren slukker også, hvis testen stopper af sig selv med en fejl,
  men ikke hvis du selv afbryder den.

## Fjernvisning fra en anden maskine

```bash
sudo ./install.sh --remote
```

Appen kører så altid som tjeneste, også efter en genstart og uden at du er logget ind.
Fra andre maskiner på netværket åbner du `http://<Odins-IP>:8090` og ser en ren
fremskridtsside: aktuelt trin, live-grafer, gennemførte trin og til sidst resultatet.
Der er ingen knapper og ingen opsætning, kun visning.

Den fulde app med opsætning, start og afbryd findes kun på Odin selv via
`http://127.0.0.1:8090` (eller menupunktet *UV Tune*).
Kør `sudo ./install.sh` uden `--remote` for at slå fjernvisning fra igen.

Bruger et andet program allerede port 8090, så vælg en anden: `sudo ./install.sh --remote --port 8095`.
Bemærk: test-ComfyUI'en har sin egen port (fx 8189/8190). Åbner du den port fra en anden
maskine, ser du ComfyUI og ikke UV Tune. Lad test-ComfyUI'en lytte på `127.0.0.1`.

## Brug

1. **Opsætning:** upload workflow-filen og tjek ComfyUI-mappen og startkommandoen.
   Kommandoen skal indeholde `--listen 127.0.0.1 --port 8189`. Testen starter sin egen
   ComfyUI-instans.
2. Tryk **Tjek system** og ret eventuelle røde punkter.
3. Luk din almindelige ComfyUI, Ollama og spil. Slå gerne MEGAsync fra under testen.
4. Tryk **Start test**. Fanen **Live** viser grafer, og **Resultater** viser tabel og anbefaling.
5. Når testen er færdig, sættes kortet tilbage til standard. Indtast det anbefalede
   power limit og voltage offset i LACT.

## Gode at vide

- **Watchdog (valgfri):** fryser maskinen helt, genstarter bundkortets hardware-watchdog
  den efter ca. 1 minut. Uden watchdog skal du selv trykke reset, og så fortsætter testen.
- **Varighed:** ca. 1–1,5 time pr. power limit plus et par minutter pr. genstart. Med 2 power
  limits ca. 2–3 timer, med 4 ca. 5 timer. Appen viser et estimat.
- **Afbryd:** brug knappen *Afbryd test*. Hvis appen ikke svarer, kan du stoppe testen med
  `sudo systemctl stop uvtune-worker`. Kortet sættes straks tilbage til standard.
- **Data:** resultater, logs, fejlbilleder og kernel-logs ligger i
  `~/.local/share/uvtune/runs/`.
- **Modellerne bliver i hukommelsen:** testen skifter mellem to faste seeds (A, B, A, B), og
  hvert seed har sit eget referencebillede. ComfyUI skal derfor regne hvert billede forfra,
  men genindlæser ikke modellerne. `--cache-none` er ikke nødvendig og fjernes automatisk.
  Kun hvis workflowet ikke har en fast seed, der kan skiftes, skal `--cache-none` bruges.
- **Workflowets længde:** sigt efter 20–60 sekunder pr. billede. Første billede efter en
  (gen)start af ComfyUI indlæser modellerne og tæller ikke med i ydelsen.
- **Gaming:** et undervolt, der er stabilt i AI, er normalt også stabilt i spil. Spil dog
  lidt ekstra de første dage, og gå 5–10 mV op, hvis et spil crasher.

## Afinstallation

```bash
sudo ./uninstall.sh           # fjerner programmet, beholder testresultater
sudo ./uninstall.sh --purge   # fjerner også resultater, logs og indstillinger
```

Scriptet afbryder en eventuel kørende test, sætter kortet tilbage til standard, starter LACT
igen, hvis testen havde sat den på pause, og fjerner begge tjenester (også fjernvisningen), `/opt/uvtune`, kommandoen
`uvtune-gui` og menupunktet. `python3-venv` og `python3-pip` bliver liggende, da andre
programmer kan bruge dem.

Copyright © 2026 L. G. H. Druidican
