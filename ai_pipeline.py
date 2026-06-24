"""
AI Pipeline -- darmowe generatory modeli i tekstur 3D.
=======================================================
Wszystko leci przez HuggingFace Spaces (gradio_client) -- bez API key,
bez kosztow, bez limitow miesiecznych. Plus opcjonalne platne:
Meshy / Tripo / Sketchfab jak masz konto.

Tools:
  ai_text_to_3d         -- prompt -> .glb (TRELLIS / Hunyuan3D / Hyper3D)
  ai_image_to_3d        -- obrazek (url/base64) -> .glb (TRELLIS image-to-3d)
  ai_gen_texture        -- prompt -> PNG (FLUX-schnell / SDXL turbo)
  ai_gen_tileable_tex   -- prompt -> seamless PNG (specjalny wzorzec)
  ai_face_to_character  -- zdjecie twarzy -> postac 3D z teksturami
  sketchfab_search      -- szuka CC0 modeli
  sketchfab_download    -- sciaga model po UID (darmowe konto + token)
  mixamo_search         -- lista postaci/animacji
  hf_pipeline_status    -- ktore spacey zyja teraz
"""
import os
import json
import time
import base64
import urllib.request
import urllib.parse
import tempfile
import traceback
from pathlib import Path
from typing import Optional, List, Dict, Any

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output"))
ASSETS_DIR = OUTPUT_DIR / "ai_assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# Konfiguracja Space'ow HuggingFace (kolejnosc = fallback chain).
# Te public Space'y daja darmowy inference bez API key,
# jak ktorys padnie -- probujemy kolejny.
HF_SPACES_TEXT_TO_3D = [
    "tencent/Hunyuan3D-2",          # SOTA, szybkie
    "JeffreyXiang/TRELLIS",         # Microsoft, swietna jakosc
    "wenkai/Hunyuan3D-2mv",         # multi-view
    "Wuvin/Unique3D",
]
HF_SPACES_IMAGE_TO_3D = [
    "JeffreyXiang/TRELLIS",
    "tencent/Hunyuan3D-2",
    "TencentARC/InstantMesh",
    "stabilityai/stable-fast-3d",
]
HF_SPACES_TEXTURE = [
    "black-forest-labs/FLUX.1-schnell",   # 1-4 step, super szybkie
    "stabilityai/stable-diffusion-3.5-large-turbo",
    "ByteDance/SDXL-Lightning",
]


# =========================================================================
#  HF SPACES via gradio_client
# =========================================================================
def _gradio():
    try:
        from gradio_client import Client, handle_file
        return Client, handle_file
    except ImportError:
        raise RuntimeError("Brak gradio_client. pip install gradio_client")


def _try_hf_text_to_3d(prompt: str, out_path: Path,
                       quality: str = "high") -> Dict[str, Any]:
    """Probuje kazdego z HF Spaces po kolei. Zwraca {space, glb_path}."""
    Client, handle_file = _gradio()
    last_err = None

    for space in HF_SPACES_TEXT_TO_3D:
        try:
            print(f"[ai] text->3d via {space}")
            client = Client(space, hf_token=os.getenv("HF_TOKEN") or None,
                            verbose=False, download_files=str(out_path.parent))
            api_info = client.view_api(return_format="dict")

            # Spaces maja rozne nazwy endpointow -- probujemy najpopularniejszych
            candidates = [
                "/generation_all", "/generate", "/run_generation",
                "/generate_mesh", "/text_to_3d", "/predict", "/run",
            ]
            available = list(api_info.get("named_endpoints", {}).keys())
            for endpoint in candidates:
                if endpoint in available:
                    try:
                        # Najczestszy podpis: (prompt,) lub (prompt, seed, steps)
                        result = client.predict(prompt, api_name=endpoint)
                        glb = _extract_glb_path(result, out_path)
                        if glb:
                            return {"space": space, "endpoint": endpoint, "glb": str(glb)}
                    except Exception as e:
                        last_err = f"{space}{endpoint}: {e}"
                        continue
            last_err = f"{space}: brak pasujacego endpointu w {available[:5]}"
        except Exception as e:
            last_err = f"{space}: {e}"
            continue

    raise RuntimeError(f"Wszystkie HF Space'y padly. Ostatni blad: {last_err}")


def _try_hf_image_to_3d(image_path: str, out_path: Path) -> Dict[str, Any]:
    Client, handle_file = _gradio()
    last_err = None
    for space in HF_SPACES_IMAGE_TO_3D:
        try:
            print(f"[ai] image->3d via {space}")
            client = Client(space, hf_token=os.getenv("HF_TOKEN") or None,
                            verbose=False, download_files=str(out_path.parent))
            api_info = client.view_api(return_format="dict")
            available = list(api_info.get("named_endpoints", {}).keys())
            candidates = [
                "/image_to_3d", "/generation_all", "/preprocess",
                "/generate", "/predict", "/run", "/generate_mvs",
            ]
            for endpoint in candidates:
                if endpoint in available:
                    try:
                        result = client.predict(handle_file(image_path), api_name=endpoint)
                        glb = _extract_glb_path(result, out_path)
                        if glb:
                            return {"space": space, "endpoint": endpoint, "glb": str(glb)}
                    except Exception as e:
                        last_err = f"{space}{endpoint}: {e}"
                        continue
            last_err = f"{space}: brak pasujacego endpointu"
        except Exception as e:
            last_err = f"{space}: {e}"
            continue

    raise RuntimeError(f"Wszystkie HF Space'y padly. Ostatni blad: {last_err}")


def _try_hf_texture(prompt: str, out_path: Path,
                    width: int = 1024, height: int = 1024) -> Dict[str, Any]:
    Client, handle_file = _gradio()
    last_err = None
    for space in HF_SPACES_TEXTURE:
        try:
            print(f"[ai] texture via {space}")
            client = Client(space, hf_token=os.getenv("HF_TOKEN") or None,
                            verbose=False, download_files=str(out_path.parent))
            api_info = client.view_api(return_format="dict")
            available = list(api_info.get("named_endpoints", {}).keys())

            for endpoint in ("/infer", "/predict", "/generate", "/run"):
                if endpoint not in available:
                    continue
                try:
                    # FLUX-schnell: (prompt, seed, randomize_seed, width, height, num_steps)
                    if "FLUX" in space or "flux" in space:
                        result = client.predict(prompt, 0, True, width, height, 4,
                                                api_name=endpoint)
                    elif "stable-diffusion-3" in space.lower() or "SD3" in space:
                        result = client.predict(prompt, "", 0, True, width, height, 5.0, 4,
                                                api_name=endpoint)
                    else:
                        result = client.predict(prompt, api_name=endpoint)
                    png = _extract_image_path(result, out_path)
                    if png:
                        return {"space": space, "image": str(png)}
                except Exception as e:
                    last_err = f"{space}{endpoint}: {e}"
                    continue
        except Exception as e:
            last_err = f"{space}: {e}"
            continue
    raise RuntimeError(f"Texture gen padlo: {last_err}")


def _extract_glb_path(result, target: Path) -> Optional[Path]:
    """Gradio zwraca rozne formaty -- wycisnijmy z tego sciezke do .glb."""
    candidates = []
    if isinstance(result, str):
        candidates.append(result)
    elif isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                for k in ("path", "url", "name", "video", "model"):
                    if k in item and isinstance(item[k], str):
                        candidates.append(item[k])
    elif isinstance(result, dict):
        for k in ("path", "url", "name", "video", "model"):
            if k in result and isinstance(result[k], str):
                candidates.append(result[k])

    for c in candidates:
        if c.endswith((".glb", ".gltf", ".obj", ".ply", ".usdz", ".fbx")):
            src = Path(c)
            if src.exists() and src.resolve() != target.resolve():
                try:
                    target.write_bytes(src.read_bytes())
                    return target
                except Exception:
                    pass
            return src if src.exists() else None
    return None


def _extract_image_path(result, target: Path) -> Optional[Path]:
    candidates = []
    if isinstance(result, str):
        candidates.append(result)
    elif isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict) and "path" in item:
                candidates.append(item["path"])
    elif isinstance(result, dict) and "path" in result:
        candidates.append(result["path"])

    for c in candidates:
        if c.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            src = Path(c)
            if src.exists() and src.resolve() != target.resolve():
                target.write_bytes(src.read_bytes())
                return target
            return src if src.exists() else None
    return None


# =========================================================================
#  SKETCHFAB (CC0 / CC-BY models, free)
# =========================================================================
SKETCHFAB_BASE = "https://api.sketchfab.com/v3"


def _sketchfab_request(path, params=None, token=None):
    url = f"{SKETCHFAB_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    token = token or os.getenv("SKETCHFAB_TOKEN")
    if token:
        req.add_header("Authorization", f"Token {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def sketchfab_search(query: str, category: str = "", license: str = "cc0",
                     max_results: int = 12) -> List[Dict[str, Any]]:
    """
    Szuka downloadable modeli. license = cc0 | by | by-sa | all.
    category np. cars-vehicles, characters-creatures, architecture, weapons.
    """
    license_map = {
        "cc0": "cc0", "by": "by", "by-sa": "by-sa", "all": "",
    }
    params = {
        "q": query, "type": "models",
        "downloadable": "true",
        "count": min(max_results, 24),
        "sort_by": "-likeCount",
    }
    if license_map.get(license):
        params["license"] = license_map[license]
    if category:
        params["categories"] = category

    data = _sketchfab_request("/search", params)
    results = []
    for m in data.get("results", []):
        results.append({
            "uid": m["uid"],
            "name": m["name"],
            "user": m.get("user", {}).get("displayName", ""),
            "license": (m.get("license") or {}).get("slug", ""),
            "vertex_count": m.get("vertexCount"),
            "face_count": m.get("faceCount"),
            "viewer_url": m.get("viewerUrl"),
            "thumbnail": (m.get("thumbnails", {}).get("images", [{}])[0]).get("url"),
        })
    return results


def sketchfab_download(uid: str, format: str = "gltf") -> Dict[str, Any]:
    """
    Pobiera model po UID. Wymaga SKETCHFAB_TOKEN w env (free konto, panel API).
    format = gltf | obj | usdz | source (jak dostepne)
    """
    if not os.getenv("SKETCHFAB_TOKEN"):
        raise RuntimeError("Ustaw SKETCHFAB_TOKEN w env (https://sketchfab.com/settings/password)")

    dl_info = _sketchfab_request(f"/models/{uid}/download")
    # Sketchfab daje signed url do ZIP-a
    if format not in dl_info:
        format = next(iter(dl_info.keys()))
    url = dl_info[format]["url"]
    out_zip = ASSETS_DIR / f"sketchfab_{uid}.zip"
    urllib.request.urlretrieve(url, out_zip)

    # rozpakuj
    import zipfile
    out_dir = ASSETS_DIR / f"sketchfab_{uid}"
    out_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(out_zip) as z:
        z.extractall(out_dir)

    # znajdz glowny plik
    main_file = None
    for ext in (".gltf", ".glb", ".obj", ".fbx"):
        for f in out_dir.rglob(f"*{ext}"):
            main_file = f
            break
        if main_file:
            break

    return {
        "uid": uid, "format": format,
        "zip": str(out_zip),
        "extracted_dir": str(out_dir),
        "main_file": str(main_file) if main_file else None,
    }


# =========================================================================
#  MIXAMO (Adobe -- free rigged characters & animations)
# =========================================================================
def mixamo_info() -> Dict[str, str]:
    """
    Mixamo nie ma public API od kiedy Adobe to przejal.
    Zwracamy instrukcje + link do oficjalnego flow:
    1. zaloguj sie na mixamo.com (free Adobe ID)
    2. wybierz postac, download FBX (Y-up, 30fps)
    3. wrzuc plik przez `import_local_file` w blender-mcp
    Mozemy tez uzyc gotowych alternatyw FREE bez logowania:
      - Quaternius (CC0)
      - Kenney (CC0)
      - Poly Haven (CC0)
    """
    return {
        "official_url": "https://www.mixamo.com",
        "alternatives_free_no_login": {
            "quaternius_characters": "https://quaternius.com/packs/ultimatemodularcharacters.html",
            "kenney_characters": "https://www.kenney.nl/assets/category:3D?sort=update",
            "polyhaven_models": "https://polyhaven.com/models",
        },
        "tip": "Sketchfab + 'rigged' w query + license=cc0 daje 1000+ darmowych rigged characters",
    }


# =========================================================================
#  POLY HAVEN (CC0 textures + HDRIs + models)
# =========================================================================
POLY_HAVEN_BASE = "https://api.polyhaven.com"


def polyhaven_search(category: str = "textures", query: str = "") -> List[Dict[str, Any]]:
    """
    category = textures | models | hdris
    Wszystko CC0 -- mozesz uzyc komercyjnie bez attribucji.
    """
    url = f"{POLY_HAVEN_BASE}/assets?t={category}"
    if query:
        url += f"&search={urllib.parse.quote(query)}"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read())
    out = []
    for slug, info in data.items():
        out.append({
            "slug": slug,
            "name": info.get("name"),
            "type": info.get("type"),
            "categories": info.get("categories"),
            "thumbnail_url": f"https://cdn.polyhaven.com/asset_img/thumbs/{slug}.png?height=256",
        })
    return out


def polyhaven_download_texture(slug: str, resolution: str = "2k",
                               maps: List[str] = None) -> Dict[str, str]:
    """Sciaga PBR teksture z Poly Haven (CC0). Zwraca sciezki do diff/norm/rough/ao."""
    maps = maps or ["diff", "nor_gl", "rough", "ao"]
    info_url = f"{POLY_HAVEN_BASE}/files/{slug}"
    with urllib.request.urlopen(info_url, timeout=20) as r:
        files = json.loads(r.read())

    out_dir = ASSETS_DIR / f"polyhaven_{slug}"
    out_dir.mkdir(exist_ok=True)
    saved = {}
    for m in maps:
        if m not in files:
            continue
        res_data = files[m].get(resolution)
        if not res_data:
            continue
        # preferuj jpg (mniej waga)
        fmt = "jpg" if "jpg" in res_data else next(iter(res_data.keys()))
        url = res_data[fmt]["url"]
        path = out_dir / f"{slug}_{m}_{resolution}.{fmt}"
        urllib.request.urlretrieve(url, path)
        saved[m] = str(path)
    return saved


def polyhaven_download_model(slug: str, resolution: str = "2k") -> Dict[str, str]:
    """Sciaga gotowy model z Poly Haven (FBX/GLB + tekstury)."""
    info_url = f"{POLY_HAVEN_BASE}/files/{slug}"
    with urllib.request.urlopen(info_url, timeout=20) as r:
        files = json.loads(r.read())
    out_dir = ASSETS_DIR / f"polyhaven_model_{slug}"
    out_dir.mkdir(exist_ok=True)
    blend_url = files.get("blend", {}).get(resolution, {}).get("blend", {}).get("url")
    fbx_url = files.get("fbx", {}).get(resolution, {}).get("fbx", {}).get("url")
    gltf_url = files.get("gltf", {}).get(resolution, {}).get("gltf", {}).get("url")
    saved = {}
    for url, key in ((fbx_url, "fbx"), (gltf_url, "gltf"), (blend_url, "blend")):
        if not url:
            continue
        ext = key
        path = out_dir / f"{slug}.{ext}"
        urllib.request.urlretrieve(url, path)
        saved[key] = str(path)
    return saved


# =========================================================================
#  MESHY (opcjonalne, platne ale ma free tier 200 credits/mc)
# =========================================================================
MESHY_BASE = "https://api.meshy.ai/openapi/v2"


def _meshy_headers():
    key = os.getenv("MESHY_API_KEY")
    if not key:
        raise RuntimeError("Ustaw MESHY_API_KEY (meshy.ai/settings/api)")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def meshy_text_to_3d(prompt: str, art_style: str = "realistic",
                     timeout: int = 600) -> Dict[str, Any]:
    """
    Meshy.ai text-to-3D. Free tier: 200 credits/mc (~10-20 modeli).
    art_style = realistic | sculpture | cartoon | low-poly
    """
    import urllib.request

    # preview
    body = json.dumps({
        "mode": "preview", "prompt": prompt,
        "art_style": art_style, "ai_model": "meshy-4",
        "topology": "quad", "target_polycount": 30000,
    }).encode()
    req = urllib.request.Request(f"{MESHY_BASE}/text-to-3d",
                                 data=body, headers=_meshy_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        task = json.loads(r.read())
    task_id = task["result"]

    # polling
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(8)
        req = urllib.request.Request(f"{MESHY_BASE}/text-to-3d/{task_id}",
                                     headers=_meshy_headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            status = json.loads(r.read())
        if status["status"] == "SUCCEEDED":
            glb_url = status["model_urls"]["glb"]
            out = ASSETS_DIR / f"meshy_{task_id}.glb"
            urllib.request.urlretrieve(glb_url, out)
            return {"task_id": task_id, "glb": str(out),
                    "thumbnail": status.get("thumbnail_url"),
                    "polycount": status.get("polycount")}
        if status["status"] == "FAILED":
            raise RuntimeError(f"Meshy failed: {status.get('task_error')}")
    raise TimeoutError("Meshy timeout")
