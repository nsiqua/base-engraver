# Pet Urn Engraver

Local web app that engraves custom text into the curved base of a 3D-printable Cat Urn
(`Domestic ShortHair_Complete_Blank.stl`) or standalone base (`Base with Screw2.stl`),
scales the urn to match the cat's live weight, and downloads the full-quality engraved STL.

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
- `python engraver.py "Charlie" cat 4.5` writes `Cat_Urn_Charlie_4.5kg.stl`
- `python engraver.py "Charlie" base 4.5` writes `Base_Charlie_4.5kg.stl`

The preview loads three.js from a CDN, so the browser needs internet access.

## Settings (top of `engraver.py`)
| Setting | Value |
|---|---|
| Font | `C:\Windows\Fonts\arial.ttf` (supports uppercase, lowercase, accents/Estonian letters, punctuation) |
| `DEFAULT_WEIGHT_KG` | `4.5 kg` (~10 lbs, typical adult Domestic Shorthair cat; slider range `1.5 – 12.0 kg` / `3.3 – 26.5 lbs`) |
| `ML_PER_KG` | `44.0 mL/kg` (~20 mL/lb: standard $1\text{ in}^3/\text{lb}$ formula + ~20% buffer for ash bag & comfortable fit) |
| `ORIG_CAPACITY_ML` | `467.2 mL` (measured usable chamber of the unscaled Cat Urn at 100% scale; linear scale factor $s = (V_{\text{req}} / 467.2)^{1/3}$) |
| `CAP_HEIGHT` | Up to 10 mm physical letter height on the scaled urn (auto-capped if the scaled base wall is under 10.6 mm tall) |
| `DEPTH` | 1 mm physical cut depth along the surface normal of the scaled urn |
| `MAX_CHARS` | 15 |
| Cat alignment | Automatically detected from the cat's ear tips and facial symmetry plane (azimuth +41.5° on the base wall), so the text sits directly beneath the cat's face |

## How it works
1. **Weight-based 3D scaling**: From the cat's weight $W$ (kg), the app computes the required usable chamber volume $V_{\text{req}} = 44 \times W\text{ mL}$ (and fluid ounces $\text{fl oz} = V_{\text{req}} / 29.5735$) and linear scale factor $s = (V_{\text{req}} / 467.2)^{1/3}$.
2. **Model decomposition**: In `Domestic ShortHair_Complete_Blank.stl`, the base shell (3,679,726 triangles) is an exact rigid transformation of `Base with Screw2.stl`. The app keeps the non-manifold cat body (3,858,582 triangles) and inner disc (256 triangles) untouched in memory, performs the boolean cut on the manifold base shell with pre-compensated cutter dimensions ($10/s\text{ mm}$ height, $1/s\text{ mm}$ depth), and scales the assembled meshes by $s$.
3. **Preview vs. Full Export**: Browser preview uses a fast vertex-clustered decimated cat mesh (~250k triangles, ~0.6 s) plus the engraved preview base, and scales live on the 220 × 220 mm build-plate grid as the user drags the weight slider. Full download exports the ~376 MB binary STL in ~4–5 s.

## Files
- `engraver.py`: core engraving logic, weight/capacity scaling, cat face-symmetry alignment, and STL assembly
- `server.py`: stdlib HTTP server (`/api/status`, `/api/preview`, `/api/engrave`) with hot-reload
- `index.html`: UI with model selector, live weight/capacity slider, three.js 3D preview, and download
