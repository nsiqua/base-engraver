# Pet Urn Engraver

Local web app that engraves custom text into the curved base of a 3D-printable Cat Urn
(`Domestic ShortHair_Complete_Blank.stl`) or standalone base (`Base with Screw2.stl`),
and downloads the full-quality engraved STL.

## Setup
1. Install Python 3.10+ and the dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Copy the blank STL models into the project folder, named:
   - `Domestic ShortHair_Complete_Blank.stl` (Cat Urn + Base, ~377 MB)
   - `Base with Screw2.stl` (Standalone Base with Screw, ~184 MB)

   The models are **not** included in this repository (they are private and exceed
   GitHub's 100 MB file limit). All `*.stl` files are git-ignored.

## Run
```
python server.py
```
Open http://localhost:8000. Both models load into memory at startup in about 15–20 s.

Command-line alternative:
- `python engraver.py "Charlie" cat` writes `Cat_Urn_Charlie.stl`
- `python engraver.py "Charlie" base` writes `Base_Charlie.stl`

The preview loads three.js from a CDN, so the browser needs internet access.

## Settings (top of `engraver.py`)
| Setting | Value |
|---|---|
| Font | `C:\Windows\Fonts\arial.ttf` (supports uppercase, lowercase, accents/Estonian letters, punctuation) |
| `CAP_HEIGHT` | 10 mm max height (standard capital height is 10 mm; if text has descenders like `y` or tall accents like `Õ`, it scales down uniformly so total height stays within 10 mm) |
| `DEPTH` | 1 mm, measured along the surface normal, so the cut is a uniform 1 mm deep on the flat wall and on the rounded top edge |
| `MAX_CHARS` | 15 |
| Cat alignment | Automatically detected from the cat's ear tips and facial symmetry plane (azimuth +41.5° on the base wall), so the text sits directly beneath the cat's face |

## How it works
1. **Model decomposition**: In `Domestic ShortHair_Complete_Blank.stl`, the base shell (3,679,726 triangles) is an exact rigid transformation of `Base with Screw2.stl`. The app keeps the non-manifold cat body (3,858,582 triangles) and inner disc (256 triangles) byte-for-byte untouched in memory, and performs the boolean cut exclusively on the manifold base shell.
2. **Text outline & normal wrap**: Text → Arial outline (matplotlib `TextPath`) → 2D cross-section → extrude 2 mm, refine, and wrap onto the measured outer-wall profile $r(z)$, offsetting along the surface normal (`manifold3d` `warp_batch`).
3. **Preview vs. Full Export**: Browser preview uses a fast vertex-clustered decimated cat mesh (~250k triangles, ~0.5 s) plus the engraved preview base. Full download combines the untouched cat body, the full-resolution engraved base, and the inner disc into a ~376 MB binary STL in ~4–5 s.

## Files
- `engraver.py`: core engraving logic, cat face-symmetry alignment, and STL assembly
- `server.py`: stdlib HTTP server (`/api/status`, `/api/preview`, `/api/engrave`) with hot-reload
- `index.html`: UI with model selector, three.js 3D preview, and download
