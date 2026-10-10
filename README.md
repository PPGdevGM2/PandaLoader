# PandaLoader

External render queue manager for DAZ Studio 6. It drives headless DAZ instances from outside, one per GPU (or all GPUs on one scene), using the `*.rq.json` queue files (compatible with ManFriday's Render Queue 3 queue files). 
Web UI on port 6969 by default, tray icon for quick actions.

**Run:** double-click `PandaLoader.exe`. A tray icon appears; the page is at <http://localhost:6969/> (the tray menu opens it). On the first start the page opens by itself with the checklist below (Settings → Setup).

## First-run checklist (fresh PC)

1. **Windows SmartScreen.** The exe is not signed. Click *More info* → *Run anyway* on the first start.
2. **Firewall prompt.** Windows asks whether PandaLoader may accept connections. Allow it to reach the page from other PCs; cancelling keeps localhost working. Leave the exe where it is, the rule is tied to its path.
3. **DAZ Studio 6, once by hand.** Start it normally, open *Render Settings → Advanced → Hardware (Photoreal Devices)*, untick **CPU** (if you don't want it to render too) and tick **every GPU that should render**. Close DAZ. The headless workers inherit exactly this selection (`-copyAppSettings`): a ticked CPU slows every render, an unticked GPU is never used.
4. **Workers.** The default is one worker on GPU 0. For each additional GPU ticked above add a worker in *Settings → General* pinned to that GPU, or switch to *One scene at a time* to let all enabled workers' GPUs render the same scene.
5. **DAZ path.** *Settings → General → DAZ Studio executable* must point at `DAZStudio.exe` when DAZ is not in the default Program Files folder. The same content library mapping as on the working PC is needed, or scenes load with missing assets.
6. **nvidia-smi** comes with the NVIDIA driver. Without it PandaLoader warns at start and shows no GPU gauges; rendering still works.
7. **Denoise (optional).** *Settings → Denoiser → Install* downloads [Denoiser OI](https://github.com/DeclanRussell/IntelOIDenoiser) into the work dir.
8. Open a queue (`.rq.json`) or create a new one, add scenes, press **Render**.

## Tested with

DAZ Studio 6.25 (Iray) on Windows 10/11 with NVIDIA GPUs. The log parsing, the per-instance
log folder (`%APPDATA%\DAZ 3D\Studio6 [<worker>]`) and the headless flags were verified against
that build; if a later DAZ changes them, those are the first things to check.
Denoising was tested with Denoiser OI 1.6.

## Files

Config, logs, job scripts and thumbnails live in `%LOCALAPPDATA%\PandaLoader` (`config.json`, `pandaloader.log`, `jobs\`).
Port: `PandaLoader.exe --port 6969`, or the tray menu → Port.

## Build from source

```bat
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pyinstaller PandaLoader.spec
```

Result: `dist\PandaLoader.exe` (single file, windowed). The version comes from
`pandaloader/__init__.py` and shows up in the page header, the tray menu and the exe's file
properties. Development run without building: `.venv\Scripts\python -m pandaloader tray --open`.

## License

MIT, see [LICENSE](LICENSE). The exe bundles [pystray](https://github.com/moses-palmer/pystray)
(LGPL-3.0, unmodified) and [Pillow](https://python-pillow.org/) (MIT-CMU); their license texts are in
[licenses/](licenses/). As required by the LGPL, pystray can be replaced with your own version by
rebuilding the exe from this source with the steps above.
