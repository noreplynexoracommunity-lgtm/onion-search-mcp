"""
Blender MCP Server (FastMCP, streamable HTTP)
==============================================
Serwer MCP ktory:
- przyjmuje wywolania od agenta Gumloop (Opus)
- wysyla komendy do headless Blendera przez TCP (port 9876)
- zwraca rezultaty (sciezki do plikow, snapshoty, status)
- streamuje viewport na zywo na /preview (WebSocket)
- eksportuje modele do formatow MTA: .dff, .txd, .col (DragonFF)
- proceduralnie generuje: pojazdy, budynki, postacie, skiny

ENV:
  BLENDER_HOST=127.0.0.1
  BLENDER_PORT=9876
  MCP_PORT=8000
"""
import os
import json
import socket
import struct
import time
import base64
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastmcp import FastMCP

BLENDER_HOST = os.getenv("BLENDER_HOST", "127.0.0.1")
BLENDER_PORT = int(os.getenv("BLENDER_PORT", "9876"))
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR_GLB = OUTPUT_DIR / "ai_glb"
ASSETS_DIR_IMG = OUTPUT_DIR / "ai_img"
ASSETS_DIR_GLB.mkdir(exist_ok=True)
ASSETS_DIR_IMG.mkdir(exist_ok=True)

mcp = FastMCP(
    name="blender-mcp",
    instructions=(
        "Blender MCP -- proceduralne modelowanie, MTA SA assets, live preview. "
        "Tworzy pojazdy, budynki, postacie, skiny i eksportuje do .dff/.txd/.col/.obj/.fbx/.glb. "
        "Kazda akcja jest natychmiast widoczna na /preview (live WebSocket viewport)."
    ),
)


# =========================================================================
#  TCP BRIDGE -> Blender addon
# =========================================================================
def _send_to_blender(payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    """Wysyla JSON do addona w Blenderze. Protokol: <uint32 len><json bytes>."""
    data = json.dumps(payload).encode("utf-8")
    header = struct.pack("!I", len(data))
    last_err = None
    # retry: addon moze sie jeszcze podnosic
    for attempt in range(6):
        try:
            with socket.create_connection((BLENDER_HOST, BLENDER_PORT), timeout=10) as s:
                s.settimeout(timeout)
                s.sendall(header + data)
                # odbierz odpowiedz
                raw_len = b""
                while len(raw_len) < 4:
                    chunk = s.recv(4 - len(raw_len))
                    if not chunk:
                        raise ConnectionError("Blender zamknal polaczenie przed naglowkiem")
                    raw_len += chunk
                resp_len = struct.unpack("!I", raw_len)[0]
                buf = b""
                while len(buf) < resp_len:
                    chunk = s.recv(min(65536, resp_len - len(buf)))
                    if not chunk:
                        raise ConnectionError("Blender zamknal polaczenie w trakcie odpowiedzi")
                    buf += chunk
                return json.loads(buf.decode("utf-8"))
        except (socket.timeout, ConnectionRefusedError, ConnectionError, OSError) as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Blender bridge unreachable po retry: {last_err}")


def _b(op: str, **kwargs) -> str:
    """Helper: wysyla operacje do Blendera, formatuje wynik jako string dla MCP."""
    resp = _send_to_blender({"op": op, "args": kwargs})
    if resp.get("status") == "error":
        return f"BLAD ({op}): {resp.get('error')}\n{resp.get('traceback', '')}"
    return json.dumps(resp.get("data", resp), indent=2, ensure_ascii=False)


# =========================================================================
#  CORE SCENE OPERATIONS
# =========================================================================
@mcp.tool()
def scene_reset() -> str:
    """Czysci scene Blendera (usuwa wszystkie obiekty, resetuje swiatlo/kamere)."""
    return _b("scene_reset")


@mcp.tool()
def scene_info() -> str:
    """Zwraca info o aktualnej scenie: lista obiektow, materialy, kamera, render settings."""
    return _b("scene_info")


@mcp.tool()
def add_primitive(
    kind: str,
    name: str = "",
    location: List[float] = (0, 0, 0),
    rotation: List[float] = (0, 0, 0),
    scale: List[float] = (1, 1, 1),
) -> str:
    """
    Dodaje prymityw. kind = cube|sphere|cylinder|cone|plane|torus|monkey|icosphere.
    """
    return _b("add_primitive", kind=kind, name=name,
              location=list(location), rotation=list(rotation), scale=list(scale))


@mcp.tool()
def transform_object(
    name: str,
    location: Optional[List[float]] = None,
    rotation: Optional[List[float]] = None,
    scale: Optional[List[float]] = None,
) -> str:
    """Zmienia transformy obiektu o nazwie `name`."""
    return _b("transform_object", name=name, location=location, rotation=rotation, scale=scale)


@mcp.tool()
def delete_object(name: str) -> str:
    """Usuwa obiekt o danej nazwie."""
    return _b("delete_object", name=name)


@mcp.tool()
def duplicate_object(name: str, new_name: str = "", offset: List[float] = (0, 0, 0)) -> str:
    """Duplikuje obiekt i przesuwa kopie o `offset`."""
    return _b("duplicate_object", name=name, new_name=new_name, offset=list(offset))


@mcp.tool()
def boolean_op(target: str, cutter: str, operation: str = "DIFFERENCE") -> str:
    """Boolean: DIFFERENCE | UNION | INTERSECT. `cutter` zostaje usuniety po operacji."""
    return _b("boolean_op", target=target, cutter=cutter, operation=operation.upper())


@mcp.tool()
def modifier_add(target: str, modifier: str, params: Dict[str, Any] = None) -> str:
    """
    Dodaje modyfikator. modifier np: SUBSURF, BEVEL, MIRROR, ARRAY, SOLIDIFY,
    DECIMATE, SMOOTH, REMESH. params -> dict z polami modyfikatora.
    """
    return _b("modifier_add", target=target, modifier=modifier.upper(), params=params or {})


@mcp.tool()
def modifier_apply_all(target: str) -> str:
    """Aplikuje wszystkie modyfikatory na obiekcie (destrukcyjnie)."""
    return _b("modifier_apply_all", target=target)


# =========================================================================
#  MATERIALS / TEXTURES / SKINS
# =========================================================================
@mcp.tool()
def material_create(
    name: str,
    base_color: List[float] = (0.8, 0.8, 0.8, 1.0),
    metallic: float = 0.0,
    roughness: float = 0.5,
    emission: List[float] = (0, 0, 0, 1),
    emission_strength: float = 0.0,
) -> str:
    """Tworzy material PBR (Principled BSDF)."""
    return _b("material_create", name=name,
              base_color=list(base_color), metallic=metallic, roughness=roughness,
              emission=list(emission), emission_strength=emission_strength)


@mcp.tool()
def material_assign(target: str, material: str) -> str:
    """Przypisuje material `material` do obiektu `target`."""
    return _b("material_assign", target=target, material=material)


@mcp.tool()
def texture_load(name: str, image_path: str, target_material: str = "") -> str:
    """
    Laduje teksture z pliku (PNG/JPG/TGA) i opcjonalnie podpina pod base color
    podanego materialu.
    """
    return _b("texture_load", name=name, image_path=image_path,
              target_material=target_material)


@mcp.tool()
def texture_paint_procedural(
    name: str,
    width: int = 512,
    height: int = 512,
    pattern: str = "noise",
    color_a: List[float] = (0.1, 0.1, 0.1, 1),
    color_b: List[float] = (0.9, 0.9, 0.9, 1),
    scale: float = 8.0,
) -> str:
    """
    Generuje teksture proceduralna w pamieci.
    pattern = noise|checker|gradient|voronoi|stripes|camo|grunge
    """
    return _b("texture_paint_procedural", name=name, width=width, height=height,
              pattern=pattern, color_a=list(color_a), color_b=list(color_b), scale=scale)


@mcp.tool()
def uv_unwrap(target: str, method: str = "SMART") -> str:
    """UV unwrap obiektu. method = SMART | UNWRAP | CUBE | SPHERE | CYLINDER."""
    return _b("uv_unwrap", target=target, method=method.upper())


@mcp.tool()
def bake_texture(target: str, bake_type: str = "DIFFUSE", resolution: int = 1024,
                 output_name: str = "") -> str:
    """
    Bakuje teksture obiektu do obrazka.
    bake_type = DIFFUSE | NORMAL | AO | ROUGHNESS | EMIT | COMBINED
    """
    return _b("bake_texture", target=target, bake_type=bake_type.upper(),
              resolution=resolution, output_name=output_name)


# =========================================================================
#  PROCEDURAL GENERATORS  (gotowe wzorce pod MTA)
# =========================================================================
@mcp.tool()
def gen_vehicle(
    name: str = "Vehicle",
    style: str = "sedan",
    length: float = 4.5,
    width: float = 1.8,
    height: float = 1.4,
    wheel_radius: float = 0.35,
    color: List[float] = (0.8, 0.1, 0.1, 1.0),
    metallic_paint: bool = True,
) -> str:
    """
    Generuje pojazd MTA-ready (body + 4 kola + szyby + zderzaki).
    style = sedan | hatchback | suv | sport | truck | van | pickup
    Wszystkie dummies (wheel_lf/rf/lr/rr, chassis) sa nazwane zgodnie z konwencja MTA.
    """
    return _b("gen_vehicle", name=name, style=style, length=length, width=width,
              height=height, wheel_radius=wheel_radius, color=list(color),
              metallic_paint=metallic_paint)


@mcp.tool()
def gen_building(
    name: str = "Building",
    style: str = "house",
    floors: int = 1,
    width: float = 8.0,
    depth: float = 6.0,
    floor_height: float = 3.0,
    windows: bool = True,
    door: bool = True,
    roof: str = "flat",
) -> str:
    """
    Generuje budynek MTA-ready.
    style = house | skyscraper | shop | warehouse | shack | apartment
    roof = flat | gable | hip | none
    """
    return _b("gen_building", name=name, style=style, floors=floors,
              width=width, depth=depth, floor_height=floor_height,
              windows=windows, door=door, roof=roof)


@mcp.tool()
def gen_character(
    name: str = "Ped",
    style: str = "civilian",
    height: float = 1.8,
    build: str = "normal",
    skin_color: List[float] = (0.85, 0.7, 0.55, 1),
    shirt_color: List[float] = (0.2, 0.3, 0.8, 1),
    pants_color: List[float] = (0.15, 0.15, 0.2, 1),
    with_armature: bool = True,
) -> str:
    """
    Generuje postac (ped) MTA-ready. style = civilian | cop | gang | businessman | soldier.
    build = thin | normal | muscular | fat
    Z armature: szkielet gotowy do animacji (kompatybilny z bonami SA).
    """
    return _b("gen_character", name=name, style=style, height=height, build=build,
              skin_color=list(skin_color), shirt_color=list(shirt_color),
              pants_color=list(pants_color), with_armature=with_armature)


@mcp.tool()
def gen_skin_variant(
    base_object: str,
    new_name: str,
    color_swap: Dict[str, List[float]] = None,
    pattern_overlay: str = "",
) -> str:
    """
    Tworzy wariant skina dla istniejacego obiektu (np. inny kolor karoserii pojazdu).
    color_swap = {"BodyPaint": [r,g,b,a], "Windows": [...]}.
    pattern_overlay = "" | "camo" | "racing_stripes" | "tribal" | "checker"
    """
    return _b("gen_skin_variant", base_object=base_object, new_name=new_name,
              color_swap=color_swap or {}, pattern_overlay=pattern_overlay)


@mcp.tool()
def gen_weapon(
    name: str = "Weapon",
    style: str = "pistol",
    barrel_length: float = 0.15,
    has_magazine: bool = True,
    has_scope: bool = False,
) -> str:
    """style = pistol | rifle | shotgun | smg | sniper | knife."""
    return _b("gen_weapon", name=name, style=style, barrel_length=barrel_length,
              has_magazine=has_magazine, has_scope=has_scope)


# =========================================================================
#  LIGHTING / CAMERA / RENDER
# =========================================================================
@mcp.tool()
def light_add(kind: str = "SUN", name: str = "Sun", location: List[float] = (5, -5, 8),
              energy: float = 3.0, color: List[float] = (1, 1, 1)) -> str:
    """kind = SUN | POINT | SPOT | AREA"""
    return _b("light_add", kind=kind.upper(), name=name, location=list(location),
              energy=energy, color=list(color))


@mcp.tool()
def camera_set(location: List[float] = (7, -7, 5), look_at: List[float] = (0, 0, 0),
               focal_length: float = 50.0) -> str:
    """Ustawia kamere na pozycji i celuje w punkt `look_at`."""
    return _b("camera_set", location=list(location), look_at=list(look_at),
              focal_length=focal_length)


@mcp.tool()
def render_image(width: int = 1280, height: int = 720, samples: int = 64,
                 engine: str = "CYCLES", output_name: str = "render.png") -> str:
    """
    Renderuje aktualna scene. engine = CYCLES | BLENDER_EEVEE_NEXT | WORKBENCH.
    Zapisuje do /app/output/<output_name> i zwraca sciezke.
    """
    return _b("render_image", width=width, height=height, samples=samples,
              engine=engine.upper(), output_name=output_name)


@mcp.tool()
def viewport_snapshot() -> str:
    """Zwraca aktualny snapshot viewportu jako base64 PNG (do podgladu w czacie)."""
    return _b("viewport_snapshot")


# =========================================================================
#  EXPORT  (MTA SA: dff/txd/col, plus uniwersalne fbx/obj/glb)
# =========================================================================
@mcp.tool()
def export_dff(target: str, output_name: str = "", with_txd: bool = True,
               with_col: bool = True) -> str:
    """
    Eksport do .dff (MTA / GTA SA) przez DragonFF.
    Dodatkowo generuje .txd (tekstury) i .col (kolizja) jesli with_txd / with_col.
    Zwraca sciezki do plikow w /app/output/.
    """
    return _b("export_dff", target=target, output_name=output_name,
              with_txd=with_txd, with_col=with_col)


@mcp.tool()
def export_fbx(target: str, output_name: str = "", embed_textures: bool = True) -> str:
    """Eksport do .fbx."""
    return _b("export_fbx", target=target, output_name=output_name,
              embed_textures=embed_textures)


@mcp.tool()
def export_obj(target: str, output_name: str = "") -> str:
    """Eksport do .obj + .mtl."""
    return _b("export_obj", target=target, output_name=output_name)


@mcp.tool()
def export_glb(target: str, output_name: str = "") -> str:
    """Eksport do .glb (gotowe pod web/Three.js)."""
    return _b("export_glb", target=target, output_name=output_name)


@mcp.tool()
def list_outputs() -> str:
    """Listuje pliki wygenerowane w /app/output/."""
    files = []
    for p in sorted(OUTPUT_DIR.rglob("*")):
        if p.is_file():
            files.append({"path": str(p), "size_kb": round(p.stat().st_size / 1024, 1)})
    return json.dumps({"output_dir": str(OUTPUT_DIR), "files": files}, indent=2)


# =========================================================================
#  AI PIPELINE  (darmowe generatory: HF, Sketchfab, Poly Haven, Mixamo)
# =========================================================================
import ai_pipeline


@mcp.tool()
def ai_text_to_3d(prompt: str, name: str = "AIModel",
                  style: str = "realistic", auto_import: bool = True) -> str:
    """
    DARMOWE text->3D przez HuggingFace (Hunyuan3D-2, TRELLIS, Unique3D).
    Generuje sliczny model .glb z samego promptu. Trwa ~30-90s.
    style = realistic | cartoon | low-poly | stylized
    auto_import = True -> od razu zaladowane do sceny Blendera.

    Przyklady promptow ktore daja DOBRE wyniki:
      "muscle car 1970s, low poly, stylized, GTA San Andreas style"
      "police officer character, full body, T-pose, low poly"
      "wooden crate, weathered, game asset"
      "AK-47 assault rifle, low poly, 1k polygons, game ready"
    """
    out = ASSETS_DIR_GLB / f"{name}.glb"
    full_prompt = f"{prompt}, {style}, game asset, clean topology, no background"
    try:
        result = ai_pipeline._try_hf_text_to_3d(full_prompt, out)
    except Exception as e:
        return f"BLAD HF text->3d: {e}\n\nAlternatywa: spróbuj ai_image_to_3d z gotowym obrazkiem (FLUX -> 3D), albo sketchfab_search."

    if auto_import and result.get("glb"):
        imp = _b("import_glb", path=result["glb"], name=name)
        return f"OK\nModel: {result['glb']}\nGenerator: {result['space']}\nImport: {imp}"
    return json.dumps(result, indent=2)


@mcp.tool()
def ai_image_to_3d(image_url: str, name: str = "AIModel",
                   auto_import: bool = True) -> str:
    """
    DARMOWE image->3D (TRELLIS, Hunyuan3D, InstantMesh).
    Wrzucasz URL do obrazka (np. wygenerowany przez ai_gen_texture lub ze stocku)
    i dostajesz ladny mesh 3D.
    """
    # sciagnij obrazek lokalnie
    import urllib.request
    src = ASSETS_DIR_IMG / f"{name}_input.png"
    urllib.request.urlretrieve(image_url, src)

    out = ASSETS_DIR_GLB / f"{name}.glb"
    try:
        result = ai_pipeline._try_hf_image_to_3d(str(src), out)
    except Exception as e:
        return f"BLAD HF image->3d: {e}"

    if auto_import and result.get("glb"):
        imp = _b("import_glb", path=result["glb"], name=name)
        return f"OK\nModel: {result['glb']}\nGenerator: {result['space']}\nImport: {imp}"
    return json.dumps(result, indent=2)


@mcp.tool()
def ai_gen_texture(prompt: str, name: str = "ai_tex",
                   width: int = 1024, height: int = 1024,
                   apply_to_object: str = "") -> str:
    """
    Generuje teksture przez FLUX-schnell / SDXL Turbo (HuggingFace, FREE).
    Jak `apply_to_object` podane -- od razu ladowane jako base color materialu.

    Tipy do promptow do TEKSTUR (nie do scen!):
      "weathered red car paint, seamless, top view"
      "denim jeans fabric, blue, seamless texture, 1024x1024"
      "old brick wall, urban grunge, photoreal"
      "muscle skin texture, normal map style, game asset"
    """
    out = ASSETS_DIR_IMG / f"{name}.png"
    try:
        result = ai_pipeline._try_hf_texture(prompt, out, width, height)
    except Exception as e:
        return f"BLAD: {e}"
    if apply_to_object:
        # zrob material + podepnij teksture
        mat_name = f"{name}_mat"
        _b("material_create", name=mat_name,
           base_color=[1, 1, 1, 1], metallic=0.0, roughness=0.5,
           emission=[0, 0, 0, 1], emission_strength=0.0)
        _b("texture_load", name=name, image_path=result["image"],
           target_material=mat_name)
        _b("material_assign", target=apply_to_object, material=mat_name)
        return f"OK -- tekstura wygenerowana ({result['space']}) i podpięta do {apply_to_object}\n{result['image']}"
    return json.dumps(result, indent=2)


@mcp.tool()
def ai_gen_character_full(
    description: str,
    name: str = "AICharacter",
    rig: bool = True,
) -> str:
    """
    PEELLNY pipeline darmowej postaci 3D:
      1. text->3D w Hunyuan3D / TRELLIS (ladne ciało z teksturami)
      2. import do Blendera
      3. auto-rig (jak rig=True) bones kompatybilne z SA
      4. cleanup, decimate jak >50k vertów
      5. eksport gotowe do export_dff() pod MTA

    description: "muscular gang member, baseball cap, hoodie, jeans, T-pose"
    """
    # 1. wygeneruj model
    prompt = f"{description}, full body character, T-pose, game ready, low poly"
    out = ASSETS_DIR_GLB / f"{name}.glb"
    try:
        result = ai_pipeline._try_hf_text_to_3d(prompt, out)
    except Exception as e:
        return f"Generator padł: {e}"

    # 2. import
    _b("import_glb", path=result["glb"], name=name)

    # 3. opcjonalny rig (Mixamo-style mapping)
    if rig:
        _b("auto_rig_character", target=name)

    # 4. cleanup
    _b("cleanup_mesh", target=name, decimate_ratio=0.6, smooth=True)

    return f"""POSTAĆ GOTOWA DO MTA
Model: {result['glb']}
Generator: {result['space']}
Obiekt w Blenderze: {name}
Rig: {'TAK (SA bones)' if rig else 'NIE'}
Dalej: export_dff(target='{name}', with_txd=True)"""


@mcp.tool()
def ai_gen_vehicle_full(
    description: str,
    name: str = "AIVehicle",
    color: List[float] = (0.8, 0.1, 0.1, 1.0),
) -> str:
    """
    Pełen pipeline darmowego pojazdu pod MTA:
      1. AI generuje ladne body
      2. import, scale do MTA
      3. dodaje 4 kola z mojej proceduralki (lepsze geometrycznie)
      4. dodaje chassis_dummy + nazwy wheel_lf/rf/lr/rr
      5. malowanie wg `color`
      6. gotowe do export_dff
    """
    prompt = f"{description}, car body only no wheels, side view, low poly game asset"
    out = ASSETS_DIR_GLB / f"{name}_body.glb"
    try:
        result = ai_pipeline._try_hf_text_to_3d(prompt, out)
    except Exception as e:
        return f"Generator padł: {e}"

    _b("import_glb", path=result["glb"], name=f"{name}_body")
    _b("assemble_vehicle_from_body", body_name=f"{name}_body", final_name=name,
       color=list(color))

    return f"""POJAZD GOTOWY DO MTA
AI body: {result['space']}
Obiekt: {name} (z {name}_body, kolami, chassis_dummy)
Dalej: viewport_set_camera_orbit() + export_dff(target='{name}')"""


@mcp.tool()
def sketchfab_find(query: str, category: str = "", license: str = "cc0",
                   max_results: int = 10) -> str:
    """
    Szuka DARMOWYCH downloadable modeli na Sketchfab.
    category: cars-vehicles | characters-creatures | architecture | weapons | furniture-home
    license: cc0 (full free) | by | by-sa | all
    """
    try:
        models = ai_pipeline.sketchfab_search(query, category, license, max_results)
    except Exception as e:
        return f"BLAD: {e}"
    return json.dumps(models, indent=2)


@mcp.tool()
def sketchfab_get(uid: str, name: str = "SketchfabModel",
                  auto_import: bool = True) -> str:
    """
    Pobiera model po UID (z sketchfab_find). Wymaga SKETCHFAB_TOKEN w env
    (free konto -> https://sketchfab.com/settings/password).
    """
    try:
        result = ai_pipeline.sketchfab_download(uid)
    except Exception as e:
        return f"BLAD: {e}"

    if auto_import and result.get("main_file"):
        main = result["main_file"]
        if main.endswith((".glb", ".gltf")):
            imp = _b("import_glb", path=main, name=name)
        elif main.endswith(".fbx"):
            imp = _b("import_fbx", path=main, name=name)
        else:
            imp = _b("import_obj", path=main, name=name)
        return f"OK\nPlik: {main}\nImport: {imp}"
    return json.dumps(result, indent=2)


@mcp.tool()
def polyhaven_find(category: str = "textures", query: str = "") -> str:
    """
    Wszystko CC0 (komercyjne, bez attribucji).
    category = textures | models | hdris
    """
    try:
        return json.dumps(ai_pipeline.polyhaven_search(category, query)[:30], indent=2)
    except Exception as e:
        return f"BLAD: {e}"


@mcp.tool()
def polyhaven_get_texture(slug: str, resolution: str = "2k",
                          apply_to_object: str = "") -> str:
    """
    Sciaga PBR teksture (diff/normal/rough/ao) z Poly Haven, opcjonalnie
    od razu podpina do obiektu jako pelny PBR material.
    """
    try:
        maps = ai_pipeline.polyhaven_download_texture(slug, resolution)
    except Exception as e:
        return f"BLAD: {e}"
    if apply_to_object and maps:
        _b("apply_pbr_textures", target=apply_to_object,
           diffuse=maps.get("diff"), normal=maps.get("nor_gl"),
           roughness=maps.get("rough"), ao=maps.get("ao"),
           material_name=f"{slug}_mat")
        return f"OK -- {slug} podpięte do {apply_to_object}\n" + json.dumps(maps, indent=2)
    return json.dumps(maps, indent=2)


@mcp.tool()
def polyhaven_get_model(slug: str, resolution: str = "2k",
                        auto_import: bool = True, name: str = "") -> str:
    """Sciaga model CC0 z Poly Haven."""
    try:
        files = ai_pipeline.polyhaven_download_model(slug, resolution)
    except Exception as e:
        return f"BLAD: {e}"
    if auto_import:
        path = files.get("gltf") or files.get("fbx") or files.get("blend")
        if path and path.endswith((".gltf", ".glb")):
            _b("import_glb", path=path, name=name or slug)
        elif path and path.endswith(".fbx"):
            _b("import_fbx", path=path, name=name or slug)
    return json.dumps(files, indent=2)


@mcp.tool()
def meshy_text_to_3d_paid(prompt: str, name: str = "MeshyModel",
                          art_style: str = "realistic",
                          auto_import: bool = True) -> str:
    """
    OPCJONALNE: Meshy.ai text->3D (200 credits/mc darmo).
    Wyzsza jakosc niz HF Spaces, ale wymaga MESHY_API_KEY w env.
    art_style = realistic | cartoon | low-poly | sculpture
    """
    try:
        result = ai_pipeline.meshy_text_to_3d(prompt, art_style)
    except Exception as e:
        return f"BLAD: {e}"
    if auto_import:
        _b("import_glb", path=result["glb"], name=name)
    return json.dumps(result, indent=2)


@mcp.tool()
def list_free_sources() -> str:
    """Wszystkie darmowe zrodla modeli i tekstur dla MTA."""
    return json.dumps({
        "ai_3d_generators_FREE_no_key": [
            "tencent/Hunyuan3D-2 (HF)",
            "JeffreyXiang/TRELLIS (HF)",
            "TencentARC/InstantMesh (HF)",
            "Wuvin/Unique3D (HF)",
        ],
        "model_libraries_CC0": {
            "polyhaven.com": "modele + textury + HDRI, wszystko CC0",
            "quaternius.com": "low-poly packs CC0 (postacie, pojazdy, broń, props)",
            "kenney.nl": "5000+ low-poly CC0",
            "sketchfab.com": "filtruj 'downloadable' + 'CC0'",
        },
        "for_mta_specifically": {
            "gtagarage.com": "30k+ gotowych .dff dla GTA SA/MTA",
            "gtainside.com": "modele pojazdów i skinów z konwersjami DFF",
            "libertycity.net": "RU community, masa pojazdów",
        },
        "image_to_texture_FREE": [
            "FLUX.1-schnell (HF, no key)",
            "Stable Diffusion 3.5 Turbo (HF, no key)",
        ],
        "rigging_FREE": "Mixamo (Adobe ID free) -> auto-rig + animacje",
    }, indent=2, ensure_ascii=False)


# =========================================================================
#  RAW PYTHON  (escape hatch dla agenta -- pelen kontrol nad bpy)
# =========================================================================
@mcp.tool()
def run_python(code: str) -> str:
    """
    Wykonuje DOWOLNY kod Python w Blenderze (bpy w scope).
    Zmienna `result` na koncu = zwrocona wartosc.
    """
    return _b("run_python", code=code)


# =========================================================================
#  LIVE PREVIEW URL
# =========================================================================
@mcp.tool()
def preview_url() -> str:
    """
    Zwraca link do live podgladu viewportu (Railway/ngrok URL + /preview).
    Wlasciwa domena ngrok jest w env PUBLIC_URL.
    """
    base = os.getenv("PUBLIC_URL", f"http://localhost:{MCP_PORT}")
    return f"{base}/preview"


@mcp.tool()
def viewport_set_camera_orbit(distance: float = 8.0, height: float = 3.0,
                              orbit_speed: float = 0.5) -> str:
    """Wlacza orbita kamere w viewporcie -- swietne dla live podgladu."""
    return _b("viewport_set_camera_orbit", distance=distance, height=height,
              orbit_speed=orbit_speed)


# =========================================================================
#  RUN
# =========================================================================
if __name__ == "__main__":
    print(f"[blender-mcp] Startuje na :{MCP_PORT}, bridge -> {BLENDER_HOST}:{BLENDER_PORT}")
    print(f"[blender-mcp] OUTPUT_DIR = {OUTPUT_DIR}")

    # Preview server uruchamiamy w osobnym watku (FastAPI + WS)
    try:
        from preview_server import start_preview_server
        t = threading.Thread(target=start_preview_server, daemon=True)
        t.start()
        print("[blender-mcp] Preview server: OK")
    except Exception as e:
        print(f"[blender-mcp] Preview server NIE wystartowal: {e}")

    mcp.run(transport="http", host="0.0.0.0", port=MCP_PORT, path="/mcp")
