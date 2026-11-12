# Urn Base Engraver (MVP)

Local web app that engraves text into the outer wall of a 3D-printable urn base
(`Base with Screw2.stl`) and downloads the full-quality engraved STL.

## Setup
1. Install Python 3.10+ and the dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Copy the blank base model into the project folder, named exactly:
   ```
   Base with Screw2.stl
   ```
   The model is **not** included in this repository: it is private and too large
   for GitHub at 184 MB. All `*.stl` files are git-ignored.

## Run
```
python server.py
```
Open http://localhost:8000. The base model takes about 15 s to load when the server starts.

Command-line alternative: `python engraver.py "Charlie"` writes `Base_Charlie.stl` next to the script.

The preview loads three.js from a CDN, so the browser needs internet access.

## Settings (top of `engraver.py`)
| Setting | Value |
|---|---|
| Font | `C:\Windows\Fonts\arial.ttf` (supports uppercase, lowercase, accents/Estonian letters, punctuation) |
| `CAP_HEIGHT` | 10 mm max height (standard capital height is 10 mm; if text has descenders like `y` or tall accents like `Õ`, it scales down uniformly so total height stays within 10 mm) |
| `DEPTH` | 1 mm, measured along the surface normal, so the cut is a uniform 1 mm deep on the flat wall and on the rounded top edge |
| `MAX_CHARS` | 15 |
| `FRONT_ANGLE` | 0 = centred on the +Y side |

## How it works
1. Text → Arial outline (matplotlib `TextPath`) → 2D cross-section
2. Extrude 2 mm, refine, then wrap it onto the measured outer-wall profile r(z),
   offsetting along the surface normal (`manifold3d` `warp_batch`)
3. Boolean subtraction from the original mesh (manifold3d). Geometry away from the
   text is left unchanged.
4. Export a binary STL

## Files
- `engraver.py`: core engraving logic and settings
- `server.py`: stdlib HTTP server (`/api/status`, `/api/preview`, `/api/engrave`)
- `index.html`: UI with a three.js preview and download
