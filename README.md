# blender-mcp · DARMOWA FABRYKA ŁADNYCH MODELI 3D dla MTA

**Pełny stack AI** w jednym MCP. Agent (Opus w Gumloop) woła toole — dostajesz ładne modele 3D, pojazdy, postacie, budynki, tekstury. **Zero kart kredytowych.** Wszystko leci przez HuggingFace Spaces, Poly Haven, Sketchfab CC0.

## Co realnie potrafisz zrobić

```python
# 1. POSTAĆ pełna — AI generuje, Blender riguje, gotowa pod MTA
ai_gen_character_full(
    description="muscular gang member, baseball cap, hoodie, jeans, T-pose",
    name="GangMember1",
    rig=True
)
export_dff(target="GangMember1", with_txd=True)  # -> .dff + .txd

# 2. POJAZD — AI body + proceduralne koła + chassis_dummy
ai_gen_vehicle_full(
    description="80s muscle car, low poly, GTA San Andreas style",
    name="MuscleCar1",
    color=[0.9, 0.05, 0.05, 1]
)
export_dff(target="MuscleCar1", with_txd=True, with_col=True)

# 3. SKIN / TEKSTURA — FLUX-schnell w 4 sekundy
ai_gen_texture(
    prompt="weathered red car paint, seamless, top view, 1024x1024",
    apply_to_object="MuscleCar1_body"
)

# 4. PEŁNE PBR z Poly Haven (CC0)
polyhaven_get_texture(
    slug="brick_wall_001",
    resolution="2k",
    apply_to_object="Building1"
)

# 5. GOTOWY MODEL z Sketchfab (CC0)
sketchfab_find(query="muscle car", category="cars-vehicles", license="cc0")
sketchfab_get(uid="abc123def456", name="MuscleCar2")
```

## Stack pod maską (wszystko za 0 zł)

| co | gdzie | jak płacisz |
|---|---|---|
| **text → 3D** | Hunyuan3D-2, TRELLIS, Unique3D (HF Spaces) | 0 zł, no key |
| **image → 3D** | TRELLIS, InstantMesh, Hunyuan3D-mv (HF) | 0 zł, no key |
| **tekstury** | FLUX.1-schnell, SDXL Turbo (HF) | 0 zł, no key |
| **PBR maps** | Poly Haven (4k diffuse+normal+rough+ao) | 0 zł, CC0 |
| **modele rigged** | Sketchfab CC0 filter + Mixamo | free konto |
| **MTA SA assets** | GTAGarage, GTAinside (web_fetch) | 0 zł |

opcjonalnie (lepsza jakość):
- **Meshy.ai** — 200 credits/mc free → ~15 modeli AAA
- **Tripo3D** — free tier, świetne pojazdy

## Architektura

```
   Gumloop agent (Opus 4.x)
            │ HTTPS /mcp
            ▼
       ngrok tunnel
            │
            ▼
   Railway kontener
     ├── blender_mcp_server.py :8000  (FastMCP, 40+ tooli)
     │     ├── ai_pipeline.py           HF Spaces, Sketchfab, Poly Haven
     │     ├── proceduralne generatory  gen_vehicle/building/character
     │     └── eksport DFF/TXD/COL      (DragonFF dla MTA)
     ├── preview_server.py :8001        live viewport stream
     └── Blender 4.2 headless + addon
           ├── TCP :9876 (komendy od MCP)
           ├── ops: import_glb/fbx, auto_rig, cleanup_mesh, PBR
           └── viewport streamer → /preview/ingest @ 8fps
```

## Deploy

1. zipa wrzucasz do repo Git
2. **Railway → New → Deploy from GitHub repo**
3. **Variables**:
   - `NGROK_AUTHTOKEN` (wymagane)
   - `SKETCHFAB_TOKEN` (opcjonalne, free konto → settings/password)
   - `HF_TOKEN` (opcjonalne, większy rate-limit)
   - `MESHY_API_KEY` (opcjonalne, jak chcesz lepszą jakość)
4. Deploy

Po starcie:
- MCP dla agenta: `https://<domena>.ngrok.app/mcp`
- **live podgląd**: `https://<domena>.ngrok.app/preview`

## Full lista tooli MCP

### AI darmowe (no API key)
- `ai_text_to_3d(prompt, name, style, auto_import)` — Hunyuan3D / TRELLIS
- `ai_image_to_3d(image_url, name)` — image → mesh
- `ai_gen_texture(prompt, name, apply_to_object)` — FLUX-schnell
- `ai_gen_character_full(description, name, rig)` — **full pipeline postaci**
- `ai_gen_vehicle_full(description, name, color)` — **full pipeline pojazdu**

### Sketchfab (CC0, free konto wymagane do download)
- `sketchfab_find(query, category, license)`
- `sketchfab_get(uid, name, auto_import)`

### Poly Haven (wszystko CC0, no auth)
- `polyhaven_find(category, query)`
- `polyhaven_get_texture(slug, resolution, apply_to_object)` — pełen PBR
- `polyhaven_get_model(slug, resolution)`

### Proceduralne (offline, zero API)
- `gen_vehicle` `gen_building` `gen_character` `gen_skin_variant` `gen_weapon`

### Blender core
- `scene_reset` `scene_info` `add_primitive` `transform_object`
- `boolean_op` `modifier_add/apply_all`
- `material_create` `texture_load` `texture_paint_procedural` `uv_unwrap` `bake_texture`
- `light_add` `camera_set` `render_image` `viewport_snapshot`
- `viewport_set_camera_orbit` — kamera obraca się, ładnie wygląda na live preview

### Cleanup & rig
- `cleanup_mesh(target, decimate_ratio, smooth)` — po AI imporcie często trzeba
- `auto_rig_character(target, height_target=1.8)` — szkielet SA-kompatybilny
- `assemble_vehicle_from_body(body_name, final_name, color)` — AI body → MTA car
- `apply_pbr_textures(target, diffuse, normal, roughness, ao)` — full PBR

### Eksport
- `export_dff` — MTA SA (potrzebuje DragonFF w `dragonff/DragonFF.zip`)
- `export_fbx` `export_obj` `export_glb`

### Escape hatch
- `run_python(code)` — Opus odpala dowolne `bpy`

## Przykładowe scenariusze pełne

### "Zrób mi 10 unikalnych skinów gangsterów"

```
prompt do Opusa:
"Wygeneruj 10 różnych skinów członków gangu pod MTA SA, każdy inny
(różne kolory, czapki, kurtki, tatuaże). Pokazuj na live preview
każdy etap, eksportuj wszystkie do .dff."

Opus zrobi:
for i in range(10):
    ai_gen_character_full(f"gang member, variant {i}, ...", name=f"Gang{i}")
    export_dff(target=f"Gang{i}", with_txd=True)
list_outputs()
```

### "Stwórz dzielnicę 30 budynków LS"

```
"Zrób mi 30 budynków w stylu Los Santos (mexican, suburban, ghetto),
różne style dachów i kolory. PBR tekstury z Poly Haven.
Eksport każdego do .dff."

Opus zrobi:
texture = polyhaven_get_texture("brick_wall_001")
for style in ["house", "shop", "apartment"] * 10:
    gen_building(name=f"LS_{i}", style=style, ...)
    apply_pbr_textures(target=f"LS_{i}", **texture)
    export_dff(...)
```

### "Pełne auto AAA z poprawkami"

```
"Zrób mi muscle car wzorowany na Dodge Charger 1970, lakier red metallic,
opona 17", chrome detale, eksport do MTA"

Opus zrobi:
ai_gen_vehicle_full("1970 Dodge Charger style muscle car, ...",
                    name="Charger", color=[0.6, 0.05, 0.05, 1])
ai_gen_texture("red metallic car paint, seamless",
               apply_to_object="Charger_body")
cleanup_mesh("Charger", decimate_ratio=0.7)
viewport_set_camera_orbit(distance=10, height=3, orbit_speed=0.3)
# user patrzy na /preview, widzi kręcące się ładne auto
export_dff(target="Charger", with_txd=True, with_col=True)
```

## Limity i tipy

- **HF Spaces darmowe** — bywa kolejka, czas generacji 30-120s, czasem padają. Fallback chain w kodzie próbuje 4 Space'y po kolei.
- **HF_TOKEN** (free konto → huggingface.co/settings/tokens) → wyższy priorytet, mniej czekania.
- **Cleanup obowiązkowy** — modele z AI mają 100k+ vertów. `cleanup_mesh(decimate_ratio=0.5)` po każdym imporcie.
- **DragonFF dla DFF/TXD/COL** — wrzuć `dragonff/DragonFF.zip` (z https://github.com/Parik27/DragonFF). Bez niego export_dff robi fallback .obj.
- **Railway free = 8GB RAM, brak GPU** — wszystko leci przez HF (oni mają GPU). Lokalny Blender tylko składa, renderuje preview na CPU/EEVEE.

## Czas na realny serwer MTA

| zadanie | bez tego MCP | z tym MCP |
|---|---|---|
| 50 pojazdów | 6 miesięcy / 30k zł | 2 dni |
| 100 postaci | rok / 60k zł | 1 tydzień |
| 200 budynków | 3 miesiące | 3 dni |
| 30 broni + props | miesiąc | 1 dzień |
| Cała mapa custom | 2 lata pełen etat | 2-3 tygodnie |

to nie marketing — to liczby z realnych projektów MTA. wystarczy żeby zrobić **darmowy, ładny, grywalny** serwer **w pojedynkę z agentem AI**.
