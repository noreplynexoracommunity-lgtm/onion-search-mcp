"""
Blender MCP Addon
==================
Dziala WEWNATRZ Blendera (headless lub GUI).
- nasluchuje TCP na 9876, przyjmuje JSON ops z serwera MCP
- wykonuje operacje w glownym watku Blendera (przez timer)
- pushuje viewport (offscreen render) jako JPEG do /preview/ingest na zywo
- laduje DragonFF jesli jest zainstalowany (eksport .dff/.txd/.col)
"""
bl_info = {
    "name": "Blender MCP Bridge",
    "author": "blender-mcp",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "category": "Interface",
}

import bpy
import bmesh
import mathutils
from mathutils import Vector, Euler, Matrix

import os
import sys
import json
import socket
import struct
import threading
import traceback
import queue
import time
import base64
import io
import math
import urllib.request

# =========================================================================
#  CONFIG (env -> defaults)
# =========================================================================
PORT = int(os.getenv("BLENDER_PORT", "9876"))
PREVIEW_INGEST = os.getenv("PREVIEW_INGEST", "http://127.0.0.1:8001/preview/ingest")
PREVIEW_FPS = float(os.getenv("PREVIEW_FPS", "8"))
PREVIEW_W = int(os.getenv("PREVIEW_W", "1280"))
PREVIEW_H = int(os.getenv("PREVIEW_H", "720"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/app/output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# =========================================================================
#  THREAD-SAFE COMMAND QUEUE  (TCP thread -> main thread via bpy.app.timers)
# =========================================================================
_CMD_Q: "queue.Queue[tuple]" = queue.Queue()


def _enqueue_and_wait(payload, timeout=180.0):
    """Wrzuca prace do main-thread queue, czeka na wynik."""
    done = threading.Event()
    box = {"result": None, "error": None}

    def _job():
        try:
            box["result"] = _dispatch(payload)
        except Exception as e:
            box["error"] = {"error": str(e), "traceback": traceback.format_exc()}
        finally:
            done.set()
        return None  # bpy.app.timers: None = nie powtarzaj

    _CMD_Q.put(_job)
    if not done.wait(timeout):
        return {"status": "error", "error": f"Timeout {timeout}s"}
    if box["error"]:
        return {"status": "error", **box["error"]}
    return {"status": "ok", "data": box["result"]}


def _pump_queue():
    """Timer w main thread -- wyciaga joby z kolejki i je wykonuje."""
    try:
        while True:
            job = _CMD_Q.get_nowait()
            try:
                job()
            except Exception:
                traceback.print_exc()
    except queue.Empty:
        pass
    return 0.05  # poll co 50ms


# =========================================================================
#  TCP SERVER  (osobny watek -- accept -> spawn handler)
# =========================================================================
class _TCPServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = None
        self.stop_flag = threading.Event()

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", PORT))
        self.sock.listen(8)
        self.sock.settimeout(1.0)
        print(f"[mcp-addon] TCP listen :{PORT}")
        while not self.stop_flag.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        try:
            self.sock.close()
        except Exception:
            pass

    def _handle(self, conn):
        try:
            conn.settimeout(300.0)
            raw_len = self._recv_n(conn, 4)
            if raw_len is None:
                return
            ln = struct.unpack("!I", raw_len)[0]
            buf = self._recv_n(conn, ln)
            if buf is None:
                return
            payload = json.loads(buf.decode("utf-8"))
            resp = _enqueue_and_wait(payload)
            data = json.dumps(resp).encode("utf-8")
            conn.sendall(struct.pack("!I", len(data)) + data)
        except Exception:
            try:
                err = json.dumps({"status": "error", "error": traceback.format_exc()}).encode()
                conn.sendall(struct.pack("!I", len(err)) + err)
            except Exception:
                pass
        finally:
            try: conn.close()
            except Exception: pass

    @staticmethod
    def _recv_n(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(min(65536, n - len(buf)))
            if not chunk:
                return None
            buf += chunk
        return buf

    def stop(self):
        self.stop_flag.set()


_SERVER: "_TCPServer | None" = None


# =========================================================================
#  VIEWPORT STREAMER  (offscreen render -> POST jpeg)
# =========================================================================
class _PreviewStreamer(threading.Thread):
    """
    Watek pomocniczy: wstrzykuje co 1/PREVIEW_FPS joba do main-thread
    ktory zrobi offscreen render i wysle JPEG do preview_server.
    """
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.orbit_t = 0.0
        self.orbit_speed = 0.0
        self.orbit_dist = 8.0
        self.orbit_height = 3.0

    def set_orbit(self, distance, height, speed):
        self.orbit_dist = distance
        self.orbit_height = height
        self.orbit_speed = speed

    def run(self):
        period = 1.0 / max(1.0, PREVIEW_FPS)
        while not self.stop_flag.is_set():
            time.sleep(period)
            done = threading.Event()
            jpg_box = {"data": None}

            def _job():
                try:
                    # opcjonalna orbita kamery
                    if self.orbit_speed > 0 and "Camera" in bpy.data.objects:
                        cam = bpy.data.objects["Camera"]
                        self.orbit_t += period * self.orbit_speed
                        a = self.orbit_t
                        cam.location = (
                            self.orbit_dist * math.cos(a),
                            self.orbit_dist * math.sin(a),
                            self.orbit_height,
                        )
                        _look_at(cam, Vector((0, 0, 0)))
                    jpg_box["data"] = _viewport_jpeg(PREVIEW_W, PREVIEW_H, quality=70)
                except Exception:
                    pass
                finally:
                    done.set()
                return None

            _CMD_Q.put(_job)
            if not done.wait(2.0):
                continue
            jpg = jpg_box["data"]
            if not jpg:
                continue
            try:
                req = urllib.request.Request(
                    PREVIEW_INGEST, data=jpg, method="POST",
                    headers={"Content-Type": "image/jpeg"},
                )
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass

    def stop(self):
        self.stop_flag.set()


_STREAMER: "_PreviewStreamer | None" = None


# =========================================================================
#  HELPERS
# =========================================================================
def _look_at(obj, target: Vector):
    direction = (target - obj.location).normalized()
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _get(name):
    o = bpy.data.objects.get(name)
    if o is None:
        raise ValueError(f"Brak obiektu: {name}")
    return o


def _select_only(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _ensure_world():
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("World")
    w = bpy.context.scene.world
    w.use_nodes = True


def _ensure_camera():
    if "Camera" not in bpy.data.objects:
        cam_data = bpy.data.cameras.new("Camera")
        cam = bpy.data.objects.new("Camera", cam_data)
        bpy.context.scene.collection.objects.link(cam)
        cam.location = (7, -7, 5)
        _look_at(cam, Vector((0, 0, 0)))
        bpy.context.scene.camera = cam
    return bpy.data.objects["Camera"]


def _viewport_jpeg(width, height, quality=75):
    """Offscreen render aktualnej kamery -> JPEG bytes."""
    scene = bpy.context.scene
    _ensure_camera()
    prev_engine = scene.render.engine
    prev_w, prev_h = scene.render.resolution_x, scene.render.resolution_y
    prev_pct = scene.render.resolution_percentage
    prev_format = scene.render.image_settings.file_format
    prev_quality = getattr(scene.render.image_settings, "quality", 90)
    prev_path = scene.render.filepath

    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT" if hasattr(bpy.app, "version") and bpy.app.version >= (4, 2, 0) else "BLENDER_EEVEE"
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "JPEG"
        scene.render.image_settings.quality = quality
        tmp = os.path.join(OUTPUT_DIR, "_preview_tmp.jpg")
        scene.render.filepath = tmp
        bpy.ops.render.render(write_still=True)
        with open(tmp, "rb") as f:
            return f.read()
    except Exception:
        # fallback: WORKBENCH (najszybszy, nie wymaga GPU)
        try:
            scene.render.engine = "BLENDER_WORKBENCH"
            tmp = os.path.join(OUTPUT_DIR, "_preview_tmp.jpg")
            scene.render.filepath = tmp
            bpy.ops.render.render(write_still=True)
            with open(tmp, "rb") as f:
                return f.read()
        except Exception:
            traceback.print_exc()
            return None
    finally:
        scene.render.engine = prev_engine
        scene.render.resolution_x = prev_w
        scene.render.resolution_y = prev_h
        scene.render.resolution_percentage = prev_pct
        scene.render.image_settings.file_format = prev_format
        try: scene.render.image_settings.quality = prev_quality
        except Exception: pass
        scene.render.filepath = prev_path


# =========================================================================
#  OPERATION DISPATCH
# =========================================================================
def _dispatch(payload):
    op = payload.get("op")
    args = payload.get("args") or {}
    fn = _OPS.get(op)
    if fn is None:
        raise ValueError(f"Nieznany op: {op}")
    return fn(**args)


# ---- core scene ----
def op_scene_reset():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.images,
                  bpy.data.cameras, bpy.data.lights, bpy.data.armatures):
        for item in list(block):
            try:
                block.remove(item)
            except Exception:
                pass
    _ensure_world()
    _ensure_camera()
    sun = bpy.data.lights.new("Sun", type="SUN")
    sun_o = bpy.data.objects.new("Sun", sun)
    bpy.context.scene.collection.objects.link(sun_o)
    sun_o.location = (5, -5, 8)
    sun.energy = 3.0
    return {"reset": True}


def op_scene_info():
    sc = bpy.context.scene
    objs = []
    for o in sc.objects:
        objs.append({
            "name": o.name, "type": o.type,
            "location": [round(x, 4) for x in o.location],
            "scale": [round(x, 4) for x in o.scale],
            "vertices": len(o.data.vertices) if o.type == "MESH" else None,
        })
    return {
        "objects": objs,
        "materials": [m.name for m in bpy.data.materials],
        "render_engine": sc.render.engine,
        "resolution": [sc.render.resolution_x, sc.render.resolution_y],
        "frame": sc.frame_current,
    }


def op_add_primitive(kind, name="", location=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1)):
    kind = kind.lower()
    fns = {
        "cube": bpy.ops.mesh.primitive_cube_add,
        "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
        "icosphere": bpy.ops.mesh.primitive_ico_sphere_add,
        "cylinder": bpy.ops.mesh.primitive_cylinder_add,
        "cone": bpy.ops.mesh.primitive_cone_add,
        "plane": bpy.ops.mesh.primitive_plane_add,
        "torus": bpy.ops.mesh.primitive_torus_add,
        "monkey": bpy.ops.mesh.primitive_monkey_add,
    }
    if kind not in fns:
        raise ValueError(f"Nieznany prymityw: {kind}")
    fns[kind](location=tuple(location), rotation=tuple(rotation))
    obj = bpy.context.active_object
    obj.scale = tuple(scale)
    if name:
        obj.name = name
    return {"name": obj.name, "type": obj.type}


def op_transform_object(name, location=None, rotation=None, scale=None):
    o = _get(name)
    if location is not None: o.location = tuple(location)
    if rotation is not None: o.rotation_euler = tuple(rotation)
    if scale is not None: o.scale = tuple(scale)
    return {"name": name,
            "location": list(o.location),
            "rotation": list(o.rotation_euler),
            "scale": list(o.scale)}


def op_delete_object(name):
    o = _get(name)
    bpy.data.objects.remove(o, do_unlink=True)
    return {"deleted": name}


def op_duplicate_object(name, new_name="", offset=(0, 0, 0)):
    src = _get(name)
    copy = src.copy()
    if src.data:
        copy.data = src.data.copy()
    bpy.context.scene.collection.objects.link(copy)
    copy.location = src.location + Vector(tuple(offset))
    if new_name:
        copy.name = new_name
    return {"name": copy.name}


def op_boolean_op(target, cutter, operation="DIFFERENCE"):
    t = _get(target); c = _get(cutter)
    mod = t.modifiers.new(name="bool", type="BOOLEAN")
    mod.operation = operation
    mod.object = c
    _select_only(t)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(c, do_unlink=True)
    return {"ok": True, "target": target}


def op_modifier_add(target, modifier, params=None):
    o = _get(target)
    m = o.modifiers.new(name=modifier.lower(), type=modifier)
    for k, v in (params or {}).items():
        try:
            setattr(m, k, v)
        except Exception:
            pass
    return {"added": m.name, "type": m.type}


def op_modifier_apply_all(target):
    o = _get(target)
    _select_only(o)
    applied = []
    for m in list(o.modifiers):
        try:
            bpy.ops.object.modifier_apply(modifier=m.name)
            applied.append(m.name)
        except Exception as e:
            print(f"modifier_apply skip {m.name}: {e}")
    return {"applied": applied}


# ---- materials ----
def _make_material(name, base_color, metallic, roughness, emission, emission_strength):
    if name in bpy.data.materials:
        bpy.data.materials.remove(bpy.data.materials[name])
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = tuple(base_color)
        bsdf.inputs["Metallic"].default_value = metallic
        bsdf.inputs["Roughness"].default_value = roughness
        if "Emission Color" in bsdf.inputs:
            bsdf.inputs["Emission Color"].default_value = tuple(emission)
        elif "Emission" in bsdf.inputs:
            bsdf.inputs["Emission"].default_value = tuple(emission)
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = emission_strength
    return mat


def op_material_create(name, base_color, metallic, roughness, emission, emission_strength):
    mat = _make_material(name, base_color, metallic, roughness, emission, emission_strength)
    return {"name": mat.name}


def op_material_assign(target, material):
    o = _get(target)
    mat = bpy.data.materials.get(material)
    if mat is None:
        raise ValueError(f"Brak materialu: {material}")
    if o.data.materials:
        o.data.materials[0] = mat
    else:
        o.data.materials.append(mat)
    return {"assigned": material, "to": target}


def op_texture_load(name, image_path, target_material=""):
    img = bpy.data.images.load(image_path, check_existing=True)
    img.name = name
    if target_material:
        mat = bpy.data.materials.get(target_material)
        if mat is None:
            raise ValueError(f"Brak materialu: {target_material}")
        mat.use_nodes = True
        nt = mat.node_tree
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        bsdf = nt.nodes.get("Principled BSDF")
        if bsdf:
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return {"image": name, "path": image_path}


def op_texture_paint_procedural(name, width, height, pattern, color_a, color_b, scale):
    img = bpy.data.images.new(name, width=width, height=height, alpha=True)
    px = [0.0] * (width * height * 4)
    import random, hashlib
    def n2(x, y):
        h = hashlib.md5(f"{x}|{y}|{scale}".encode()).digest()
        return h[0] / 255.0

    for y in range(height):
        for x in range(width):
            u = x / max(1, width - 1)
            v = y / max(1, height - 1)
            t = 0.0
            if pattern == "noise":
                t = n2(int(x / max(1, scale)), int(y / max(1, scale)))
            elif pattern == "checker":
                t = 1.0 if ((int(u * scale) + int(v * scale)) % 2) else 0.0
            elif pattern == "gradient":
                t = u
            elif pattern == "stripes":
                t = 1.0 if int(u * scale) % 2 else 0.0
            elif pattern == "voronoi":
                # cheap voronoi: distance do najblizszego z X losowych punktow
                t = (math.sin(u * scale * 6.283) * math.cos(v * scale * 6.283) + 1) * 0.5
            elif pattern == "camo":
                t = (n2(int(x/scale), int(y/scale)) +
                     n2(int(x/scale*2), int(y/scale*2)) * 0.5) / 1.5
                t = 0.0 if t < 0.45 else 1.0
            elif pattern == "grunge":
                t = max(0.0, min(1.0,
                    n2(int(x/2), int(y/2)) * 0.6 +
                    n2(int(x/8), int(y/8)) * 0.4))
            i = (y * width + x) * 4
            r = color_a[0] * (1 - t) + color_b[0] * t
            g = color_a[1] * (1 - t) + color_b[1] * t
            b = color_a[2] * (1 - t) + color_b[2] * t
            a = color_a[3] * (1 - t) + color_b[3] * t
            px[i]=r; px[i+1]=g; px[i+2]=b; px[i+3]=a
    img.pixels = px
    img.pack()
    return {"image": img.name, "size": [width, height], "pattern": pattern}


def op_uv_unwrap(target, method="SMART"):
    o = _get(target)
    _select_only(o)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    if method == "SMART":
        bpy.ops.uv.smart_project(angle_limit=math.radians(66))
    elif method == "UNWRAP":
        bpy.ops.uv.unwrap()
    elif method == "CUBE":
        bpy.ops.uv.cube_project()
    elif method == "SPHERE":
        bpy.ops.uv.sphere_project()
    elif method == "CYLINDER":
        bpy.ops.uv.cylinder_project()
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"unwrapped": target, "method": method}


def op_bake_texture(target, bake_type="DIFFUSE", resolution=1024, output_name=""):
    o = _get(target)
    if not o.data.materials:
        raise ValueError("Obiekt nie ma materialu")
    mat = o.data.materials[0]
    mat.use_nodes = True
    nt = mat.node_tree
    img_name = output_name or f"{target}_bake_{bake_type}"
    img = bpy.data.images.new(img_name, resolution, resolution)
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    nt.nodes.active = tex
    _select_only(o)
    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.samples = 32
    bpy.ops.object.bake(type=bake_type)
    path = os.path.join(OUTPUT_DIR, f"{img_name}.png")
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    return {"baked": path, "type": bake_type}


# ---- generators ----
def op_gen_vehicle(name, style, length, width, height, wheel_radius, color, metallic_paint):
    # body
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, height/2 + wheel_radius))
    body = bpy.context.active_object
    body.scale = (length/2, width/2, height/2)
    body.name = f"{name}_body"
    bpy.ops.object.transform_apply(scale=True)

    # cabin profile depending on style
    if style in ("sedan", "hatchback", "suv", "sport"):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(length*0.05, 0, height + wheel_radius + 0.15))
        cabin = bpy.context.active_object
        cab_l = length * (0.55 if style == "sedan" else 0.65 if style == "hatchback" else 0.7 if style == "suv" else 0.45)
        cab_h = height * (0.55 if style != "sport" else 0.45)
        cabin.scale = (cab_l/2, (width*0.92)/2, cab_h/2)
        cabin.name = f"{name}_cabin"
        bpy.ops.object.transform_apply(scale=True)
        # bevel cabin
        m = cabin.modifiers.new("bevel", "BEVEL")
        m.width = 0.08; m.segments = 3
        _select_only(cabin)
        bpy.ops.object.modifier_apply(modifier="bevel")
    elif style == "truck":
        bpy.ops.mesh.primitive_cube_add(size=1, location=(-length*0.3, 0, height + wheel_radius + 0.2))
        cabin = bpy.context.active_object
        cabin.scale = (length*0.25, (width*0.95)/2, height*0.6)
        cabin.name = f"{name}_cabin"
        bpy.ops.object.transform_apply(scale=True)
    else:  # van / pickup
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, height + wheel_radius + 0.15))
        cabin = bpy.context.active_object
        cabin.scale = (length*0.4, width*0.45, height*0.45)
        cabin.name = f"{name}_cabin"
        bpy.ops.object.transform_apply(scale=True)

    # bevel body
    m = body.modifiers.new("bevel", "BEVEL")
    m.width = 0.1; m.segments = 4
    _select_only(body)
    bpy.ops.object.modifier_apply(modifier="bevel")

    # wheels (MTA naming: wheel_lf, wheel_rf, wheel_lr, wheel_rr)
    wheel_y = (width/2) - 0.05
    wheel_x = length/2 - wheel_radius * 1.8
    wheel_z = wheel_radius
    wheel_positions = {
        "wheel_lf": ( wheel_x,  wheel_y, wheel_z),
        "wheel_rf": ( wheel_x, -wheel_y, wheel_z),
        "wheel_lr": (-wheel_x,  wheel_y, wheel_z),
        "wheel_rr": (-wheel_x, -wheel_y, wheel_z),
    }
    wheels = []
    for wname, pos in wheel_positions.items():
        bpy.ops.mesh.primitive_cylinder_add(radius=wheel_radius, depth=0.2,
                                            location=pos, rotation=(math.radians(90), 0, 0))
        w = bpy.context.active_object
        w.name = f"{name}_{wname}"
        wheels.append(w.name)

    # paint material
    paint = _make_material(
        f"{name}_paint", color, 0.8 if metallic_paint else 0.0,
        0.25 if metallic_paint else 0.5, (0,0,0,1), 0.0)
    glass = _make_material(f"{name}_glass", (0.05, 0.1, 0.15, 1), 0.0, 0.05, (0,0,0,1), 0.0)
    rubber = _make_material(f"{name}_rubber", (0.04, 0.04, 0.04, 1), 0.0, 0.9, (0,0,0,1), 0.0)

    # assign
    for o, mat in ((body, paint), (cabin, glass)):
        if o.data.materials:
            o.data.materials[0] = mat
        else:
            o.data.materials.append(mat)
    for wn in wheels:
        wo = bpy.data.objects[wn]
        wo.data.materials.append(rubber)

    # chassis empty (MTA dummy)
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    chassis = bpy.context.active_object
    chassis.name = f"{name}_chassis_dummy"

    # parent everything to chassis
    for n in [body.name, cabin.name] + wheels:
        o = bpy.data.objects[n]
        o.parent = chassis

    return {"vehicle": name, "style": style,
            "body": body.name, "cabin": cabin.name, "wheels": wheels,
            "chassis_dummy": chassis.name}


def op_gen_building(name, style, floors, width, depth, floor_height, windows, door, roof):
    total_h = floors * floor_height
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, total_h/2))
    base = bpy.context.active_object
    base.scale = (width/2, depth/2, total_h/2)
    base.name = f"{name}_base"
    bpy.ops.object.transform_apply(scale=True)

    pieces = [base.name]

    # roof
    if roof == "gable":
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, total_h + floor_height*0.3))
        r = bpy.context.active_object
        r.scale = (width/2, depth/2, floor_height*0.3)
        bpy.ops.object.transform_apply(scale=True)
        # convert top to triangular prism via edit mode
        _select_only(r)
        bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(r.data)
        for v in bm.verts:
            if v.co.z > 0:
                v.co.y = 0
        bmesh.update_edit_mesh(r.data)
        bpy.ops.object.mode_set(mode="OBJECT")
        r.name = f"{name}_roof"
        pieces.append(r.name)
    elif roof == "hip":
        bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=max(width, depth)*0.7,
                                        depth=floor_height*0.5,
                                        location=(0, 0, total_h + floor_height*0.25))
        r = bpy.context.active_object
        r.rotation_euler = (0, 0, math.radians(45))
        r.scale = (width/(max(width,depth)*1.4), depth/(max(width,depth)*1.4), 1)
        bpy.ops.object.transform_apply(scale=True, rotation=True)
        r.name = f"{name}_roof"
        pieces.append(r.name)

    # door
    if door:
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, depth/2 + 0.01, 1.0))
        d = bpy.context.active_object
        d.scale = (0.45, 0.05, 1.0)
        bpy.ops.object.transform_apply(scale=True)
        d.name = f"{name}_door"
        pieces.append(d.name)

    # windows row per floor
    if windows:
        win_per_side = max(1, int(width // 2.5))
        for f in range(floors):
            y = depth/2 + 0.01
            cz = f * floor_height + floor_height * 0.6
            spacing = width / (win_per_side + 1)
            for i in range(win_per_side):
                cx = -width/2 + spacing * (i + 1)
                bpy.ops.mesh.primitive_cube_add(size=1, location=(cx, y, cz))
                w = bpy.context.active_object
                w.scale = (0.5, 0.04, 0.5)
                bpy.ops.object.transform_apply(scale=True)
                w.name = f"{name}_win_{f}_{i}"
                pieces.append(w.name)

    # materiały
    wall = _make_material(f"{name}_wall",
                          (0.7, 0.65, 0.6, 1) if style == "house" else (0.5, 0.55, 0.6, 1),
                          0.0, 0.8, (0,0,0,1), 0.0)
    glass = _make_material(f"{name}_glass", (0.2, 0.4, 0.6, 1), 0.3, 0.1, (0,0,0,1), 0.0)
    wood = _make_material(f"{name}_wood", (0.35, 0.2, 0.1, 1), 0.0, 0.7, (0,0,0,1), 0.0)

    for n in pieces:
        o = bpy.data.objects[n]
        if "win" in n:
            o.data.materials.append(glass)
        elif "door" in n:
            o.data.materials.append(wood)
        else:
            if o.data.materials:
                o.data.materials[0] = wall
            else:
                o.data.materials.append(wall)

    return {"building": name, "style": style, "floors": floors, "pieces": pieces}


def op_gen_character(name, style, height, build, skin_color, shirt_color, pants_color, with_armature):
    bw = {"thin": 0.32, "normal": 0.42, "muscular": 0.5, "fat": 0.6}.get(build, 0.42)
    # torso
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, height*0.55))
    torso = bpy.context.active_object
    torso.scale = (bw, bw*0.55, height*0.28)
    bpy.ops.object.transform_apply(scale=True)
    torso.name = f"{name}_torso"
    m = torso.modifiers.new("bevel", "BEVEL"); m.width = 0.05; m.segments = 3
    _select_only(torso); bpy.ops.object.modifier_apply(modifier="bevel")

    # head
    bpy.ops.mesh.primitive_uv_sphere_add(radius=bw*0.55, location=(0, 0, height*0.92))
    head = bpy.context.active_object
    head.name = f"{name}_head"

    # arms
    arms = []
    for side, sx in (("L", 1), ("R", -1)):
        bpy.ops.mesh.primitive_cylinder_add(radius=bw*0.22, depth=height*0.4,
                                            location=(sx*(bw+0.05), 0, height*0.55))
        a = bpy.context.active_object
        a.name = f"{name}_arm_{side}"
        arms.append(a.name)

    # legs
    legs = []
    for side, sx in (("L", 1), ("R", -1)):
        bpy.ops.mesh.primitive_cylinder_add(radius=bw*0.28, depth=height*0.45,
                                            location=(sx*bw*0.45, 0, height*0.22))
        l = bpy.context.active_object
        l.name = f"{name}_leg_{side}"
        legs.append(l.name)

    # materiały
    skin = _make_material(f"{name}_skin", skin_color, 0.0, 0.55, (0,0,0,1), 0.0)
    shirt = _make_material(f"{name}_shirt", shirt_color, 0.0, 0.7, (0,0,0,1), 0.0)
    pants = _make_material(f"{name}_pants", pants_color, 0.0, 0.8, (0,0,0,1), 0.0)

    bpy.data.objects[f"{name}_head"].data.materials.append(skin)
    bpy.data.objects[f"{name}_torso"].data.materials.append(shirt)
    for an in arms:
        bpy.data.objects[an].data.materials.append(shirt)
    for ln in legs:
        bpy.data.objects[ln].data.materials.append(pants)

    armature_name = None
    if with_armature:
        bpy.ops.object.armature_add(location=(0, 0, 0))
        arm = bpy.context.active_object
        arm.name = f"{name}_armature"
        armature_name = arm.name
        bpy.ops.object.mode_set(mode="EDIT")
        ebones = arm.data.edit_bones
        # podstawowy szkielet GTA-like
        ebones.remove(ebones[0])
        def bone(n, head, tail, parent=None):
            b = ebones.new(n)
            b.head = head; b.tail = tail
            if parent: b.parent = ebones[parent]
            return b
        bone("pelvis", (0, 0, height*0.45), (0, 0, height*0.6))
        bone("spine",  (0, 0, height*0.6), (0, 0, height*0.78), "pelvis")
        bone("neck",   (0, 0, height*0.78), (0, 0, height*0.88), "spine")
        bone("head",   (0, 0, height*0.88), (0, 0, height*1.0), "neck")
        bone("clavicle_L", (0, 0, height*0.78), (bw+0.05, 0, height*0.78), "spine")
        bone("upper_arm_L", (bw+0.05, 0, height*0.78), (bw+0.05, 0, height*0.55), "clavicle_L")
        bone("forearm_L", (bw+0.05, 0, height*0.55), (bw+0.05, 0, height*0.35), "upper_arm_L")
        bone("clavicle_R", (0, 0, height*0.78), (-(bw+0.05), 0, height*0.78), "spine")
        bone("upper_arm_R", (-(bw+0.05), 0, height*0.78), (-(bw+0.05), 0, height*0.55), "clavicle_R")
        bone("forearm_R", (-(bw+0.05), 0, height*0.55), (-(bw+0.05), 0, height*0.35), "upper_arm_R")
        bone("upper_leg_L", (bw*0.45, 0, height*0.45), (bw*0.45, 0, height*0.22), "pelvis")
        bone("lower_leg_L", (bw*0.45, 0, height*0.22), (bw*0.45, 0, 0.01), "upper_leg_L")
        bone("upper_leg_R", (-bw*0.45, 0, height*0.45), (-bw*0.45, 0, height*0.22), "pelvis")
        bone("lower_leg_R", (-bw*0.45, 0, height*0.22), (-bw*0.45, 0, 0.01), "upper_leg_R")
        bpy.ops.object.mode_set(mode="OBJECT")

    return {"character": name, "parts": [f"{name}_head", f"{name}_torso"] + arms + legs,
            "armature": armature_name, "style": style, "build": build}


def op_gen_skin_variant(base_object, new_name, color_swap=None, pattern_overlay=""):
    src = _get(base_object)
    copy = src.copy()
    if src.data: copy.data = src.data.copy()
    bpy.context.scene.collection.objects.link(copy)
    copy.name = new_name

    if color_swap:
        for mat_name, color in color_swap.items():
            mat = bpy.data.materials.get(mat_name)
            if mat and mat.node_tree:
                bsdf = mat.node_tree.nodes.get("Principled BSDF")
                if bsdf:
                    bsdf.inputs["Base Color"].default_value = tuple(color)

    return {"variant": new_name, "from": base_object, "pattern": pattern_overlay}


def op_gen_weapon(name, style, barrel_length, has_magazine, has_scope):
    # body
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
    body = bpy.context.active_object
    if style == "pistol":
        body.scale = (0.08, 0.25, 0.15)
    elif style == "rifle":
        body.scale = (0.06, 0.7, 0.1)
    elif style == "shotgun":
        body.scale = (0.07, 0.8, 0.1)
    elif style == "smg":
        body.scale = (0.06, 0.35, 0.12)
    elif style == "sniper":
        body.scale = (0.06, 1.0, 0.1)
    else:
        body.scale = (0.03, 0.2, 0.04)
    bpy.ops.object.transform_apply(scale=True)
    body.name = f"{name}_body"

    pieces = [body.name]

    if style != "knife":
        # barrel
        bpy.ops.mesh.primitive_cylinder_add(radius=0.015, depth=barrel_length,
                                            location=(0, body.dimensions.y/2 + barrel_length/2, 0.02),
                                            rotation=(math.radians(90), 0, 0))
        barrel = bpy.context.active_object
        barrel.name = f"{name}_barrel"
        pieces.append(barrel.name)

    if has_magazine and style not in ("knife", "shotgun"):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(0, -body.dimensions.y*0.1, -0.08))
        mag = bpy.context.active_object
        mag.scale = (0.03, 0.04, 0.1)
        bpy.ops.object.transform_apply(scale=True)
        mag.name = f"{name}_mag"
        pieces.append(mag.name)

    if has_scope and style in ("rifle", "sniper"):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.025, depth=0.2,
                                            location=(0, 0, body.dimensions.z/2 + 0.04),
                                            rotation=(math.radians(90), 0, 0))
        scope = bpy.context.active_object
        scope.name = f"{name}_scope"
        pieces.append(scope.name)

    metal = _make_material(f"{name}_metal", (0.15, 0.15, 0.17, 1), 0.9, 0.3, (0,0,0,1), 0.0)
    for p in pieces:
        o = bpy.data.objects[p]
        if o.data.materials:
            o.data.materials[0] = metal
        else:
            o.data.materials.append(metal)

    return {"weapon": name, "style": style, "pieces": pieces}


# ---- lights / camera / render ----
def op_light_add(kind, name, location, energy, color):
    light = bpy.data.lights.new(name, type=kind)
    light.energy = energy
    light.color = tuple(color)
    obj = bpy.data.objects.new(name, light)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = tuple(location)
    return {"light": name, "kind": kind}


def op_camera_set(location, look_at, focal_length):
    cam = _ensure_camera()
    cam.location = tuple(location)
    cam.data.lens = focal_length
    _look_at(cam, Vector(tuple(look_at)))
    bpy.context.scene.camera = cam
    return {"camera": cam.name, "location": list(cam.location)}


def op_render_image(width, height, samples, engine, output_name):
    scene = bpy.context.scene
    scene.render.engine = engine
    if engine == "CYCLES":
        scene.cycles.samples = samples
        scene.cycles.device = "CPU"  # Railway nie ma GPU
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    path = os.path.join(OUTPUT_DIR, output_name)
    scene.render.filepath = path
    _ensure_camera()
    bpy.ops.render.render(write_still=True)
    return {"output": path, "engine": engine, "size": [width, height]}


def op_viewport_snapshot():
    jpg = _viewport_jpeg(960, 540, quality=80)
    if not jpg:
        return {"error": "no frame"}
    return {"base64": base64.b64encode(jpg).decode(), "mime": "image/jpeg"}


def op_viewport_set_camera_orbit(distance, height, orbit_speed):
    global _STREAMER
    if _STREAMER:
        _STREAMER.set_orbit(distance, height, orbit_speed)
    return {"orbit": True, "distance": distance, "height": height, "speed": orbit_speed}


# ---- export ----
def _select_with_children(name):
    o = _get(name)
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    for c in o.children_recursive:
        c.select_set(True)
    bpy.context.view_layer.objects.active = o


def op_export_dff(target, output_name="", with_txd=True, with_col=True):
    name = output_name or target
    dff_path = os.path.join(OUTPUT_DIR, f"{name}.dff")
    _select_with_children(target)

    # DragonFF -- jak jest, uzyj; jak nie ma, fallback OBJ + info
    try:
        bpy.ops.export_dff.scene(filepath=dff_path, export_version="0x36003")
        result = {"dff": dff_path}
    except Exception as e:
        # fallback: OBJ + komunikat
        obj_path = os.path.join(OUTPUT_DIR, f"{name}.obj")
        bpy.ops.wm.obj_export(filepath=obj_path, export_selected_objects=True)
        result = {"dff": None, "fallback_obj": obj_path,
                  "note": f"DragonFF nie jest dostepny ({e}). Zainstaluj addon DragonFF lub uzyj OBJ + RW Analyze."}

    if with_txd:
        txd_path = os.path.join(OUTPUT_DIR, f"{name}.txd.info")
        with open(txd_path, "w") as f:
            f.write("TXD: uzyj DragonFF lub Magic.TXD do upakowania PNG z OUTPUT_DIR\n")
        result["txd_info"] = txd_path

    if with_col:
        col_path = os.path.join(OUTPUT_DIR, f"{name}.col.info")
        with open(col_path, "w") as f:
            f.write("COL: uzyj DragonFF (Collision tools) lub Steve's Collision Editor\n")
        result["col_info"] = col_path

    return result


def op_export_fbx(target, output_name="", embed_textures=True):
    name = output_name or target
    path = os.path.join(OUTPUT_DIR, f"{name}.fbx")
    _select_with_children(target)
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True,
                             embed_textures=embed_textures, path_mode="COPY")
    return {"fbx": path}


def op_export_obj(target, output_name=""):
    name = output_name or target
    path = os.path.join(OUTPUT_DIR, f"{name}.obj")
    _select_with_children(target)
    try:
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True)
    except AttributeError:
        bpy.ops.export_scene.obj(filepath=path, use_selection=True)
    return {"obj": path}


def op_export_glb(target, output_name=""):
    name = output_name or target
    path = os.path.join(OUTPUT_DIR, f"{name}.glb")
    _select_with_children(target)
    bpy.ops.export_scene.gltf(filepath=path, use_selection=True, export_format="GLB")
    return {"glb": path}


# ---- import (z AI / Sketchfab / Poly Haven) ----
def _rename_imported(name):
    """Po imporcie GLB/FBX zwracamy wszystkie nowe obiekty pod jednym parentem."""
    selected = list(bpy.context.selected_objects)
    if not selected:
        return None
    if len(selected) == 1:
        selected[0].name = name
        return selected[0].name
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    parent = bpy.context.active_object
    parent.name = name
    for o in selected:
        if o != parent:
            o.parent = parent
    return name


def op_import_glb(path, name="Imported"):
    bpy.ops.object.select_all(action="DESELECT")
    pre = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    post = set(o.name for o in bpy.data.objects)
    new = [bpy.data.objects[n] for n in (post - pre)]
    for o in new: o.select_set(True)
    if new:
        bpy.context.view_layer.objects.active = new[0]
    final = _rename_imported(name)
    return {"imported": final, "objects": [o.name for o in new]}


def op_import_fbx(path, name="Imported"):
    bpy.ops.object.select_all(action="DESELECT")
    pre = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path)
    post = set(o.name for o in bpy.data.objects)
    new = [bpy.data.objects[n] for n in (post - pre)]
    for o in new: o.select_set(True)
    if new:
        bpy.context.view_layer.objects.active = new[0]
    final = _rename_imported(name)
    return {"imported": final, "objects": [o.name for o in new]}


def op_import_obj(path, name="Imported"):
    bpy.ops.object.select_all(action="DESELECT")
    pre = set(o.name for o in bpy.data.objects)
    try:
        bpy.ops.wm.obj_import(filepath=path)
    except AttributeError:
        bpy.ops.import_scene.obj(filepath=path)
    post = set(o.name for o in bpy.data.objects)
    new = [bpy.data.objects[n] for n in (post - pre)]
    for o in new: o.select_set(True)
    if new:
        bpy.context.view_layer.objects.active = new[0]
    final = _rename_imported(name)
    return {"imported": final, "objects": [o.name for o in new]}


# ---- cleanup (po imporcie z AI -- czesto za duzo polygonow i smieciowe normals) ----
def op_cleanup_mesh(target, decimate_ratio=0.5, smooth=True, merge_distance=0.001):
    o = _get(target)
    targets = [o] + o.children_recursive
    cleaned = []
    for t in targets:
        if t.type != "MESH":
            continue
        _select_only(t)
        # merge by distance
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.remove_doubles(threshold=merge_distance)
        bpy.ops.mesh.normals_make_consistent(inside=False)
        if smooth:
            bpy.ops.mesh.faces_shade_smooth()
        bpy.ops.object.mode_set(mode="OBJECT")
        # decimate
        if decimate_ratio < 1.0:
            m = t.modifiers.new("decimate", "DECIMATE")
            m.ratio = decimate_ratio
            bpy.ops.object.modifier_apply(modifier="decimate")
        cleaned.append({"name": t.name, "verts": len(t.data.vertices),
                        "polys": len(t.data.polygons)})
    return {"cleaned": cleaned}


# ---- auto-rig (Mixamo-like, kompatybilne z SA bone names) ----
SA_BONES = [
    ("Root",        (0, 0, 0.0),         (0, 0, 0.45)),
    ("Pelvis",      (0, 0, 0.45),        (0, 0, 0.6),     "Root"),
    ("Spine",       (0, 0, 0.6),         (0, 0, 0.78),    "Pelvis"),
    ("Spine1",      (0, 0, 0.78),        (0, 0, 0.88),    "Spine"),
    ("Neck",        (0, 0, 0.88),        (0, 0, 0.93),    "Spine1"),
    ("Head",        (0, 0, 0.93),        (0, 0, 1.05),    "Neck"),
    ("L Clavicle",  (0.04, 0, 0.85),     (0.18, 0, 0.85), "Spine1"),
    ("L UpperArm",  (0.18, 0, 0.85),     (0.4, 0, 0.6),   "L Clavicle"),
    ("L Forearm",   (0.4, 0, 0.6),       (0.6, 0, 0.4),   "L UpperArm"),
    ("L Hand",      (0.6, 0, 0.4),       (0.7, 0, 0.32),  "L Forearm"),
    ("R Clavicle",  (-0.04, 0, 0.85),    (-0.18, 0, 0.85),"Spine1"),
    ("R UpperArm",  (-0.18, 0, 0.85),    (-0.4, 0, 0.6),  "R Clavicle"),
    ("R Forearm",   (-0.4, 0, 0.6),      (-0.6, 0, 0.4),  "R UpperArm"),
    ("R Hand",      (-0.6, 0, 0.4),      (-0.7, 0, 0.32), "R Forearm"),
    ("L Thigh",     (0.12, 0, 0.45),     (0.13, 0, 0.22), "Pelvis"),
    ("L Calf",      (0.13, 0, 0.22),     (0.14, 0, 0.05), "L Thigh"),
    ("L Foot",      (0.14, 0, 0.05),     (0.14, 0.12, 0.02), "L Calf"),
    ("R Thigh",     (-0.12, 0, 0.45),    (-0.13, 0, 0.22),"Pelvis"),
    ("R Calf",      (-0.13, 0, 0.22),    (-0.14, 0, 0.05),"R Thigh"),
    ("R Foot",      (-0.14, 0, 0.05),    (-0.14, 0.12, 0.02), "R Calf"),
]


def op_auto_rig_character(target, height_target=1.8):
    """Tworzy armature SA-kompatybilna i parentuje mesh przez automatic weights."""
    o = _get(target)
    # 1. wyskaluj mesh do height_target wzgledem aktualnego bounding box
    meshes = [o] + [c for c in o.children_recursive if c.type == "MESH"]
    if not meshes:
        raise ValueError("Brak mesha w targecie")
    # AABB
    z_min = min((m.matrix_world @ Vector(corner)).z for m in meshes for corner in m.bound_box)
    z_max = max((m.matrix_world @ Vector(corner)).z for m in meshes for corner in m.bound_box)
    current_h = z_max - z_min
    if current_h > 0.001:
        scale_factor = height_target / current_h
        o.scale = (o.scale.x * scale_factor, o.scale.y * scale_factor, o.scale.z * scale_factor)
        # ustaw stopy na z=0
        bpy.context.view_layer.update()
        z_min = min((m.matrix_world @ Vector(corner)).z for m in meshes for corner in m.bound_box)
        o.location.z -= z_min

    # 2. armature
    bpy.ops.object.armature_add(location=(0, 0, 0))
    arm = bpy.context.active_object
    arm.name = f"{target}_armature"
    bpy.ops.object.mode_set(mode="EDIT")
    ebones = arm.data.edit_bones
    ebones.remove(ebones[0])
    created = {}
    for entry in SA_BONES:
        bname = entry[0]
        head = Vector(entry[1]) * height_target / 1.8
        tail = Vector(entry[2]) * height_target / 1.8
        b = ebones.new(bname)
        b.head, b.tail = head, tail
        if len(entry) > 3:
            parent_name = entry[3]
            if parent_name in created:
                b.parent = created[parent_name]
        created[bname] = b
    bpy.ops.object.mode_set(mode="OBJECT")

    # 3. parent mesh z automatic weights
    for m in meshes:
        m.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as e:
        return {"armature": arm.name, "rigged": False, "note": f"auto-weight padl: {e}"}

    return {"armature": arm.name, "bones": list(created.keys()), "rigged": True}


# ---- assemble_vehicle_from_body: bierze AI body, dorzuca kola/dummy ----
def op_assemble_vehicle_from_body(body_name, final_name, color=(0.8, 0.1, 0.1, 1)):
    body = _get(body_name)
    # zmierz boundbox
    bb = [body.matrix_world @ Vector(c) for c in body.bound_box]
    xs = [v.x for v in bb]; ys = [v.y for v in bb]; zs = [v.z for v in bb]
    length = max(xs) - min(xs)
    width = max(ys) - min(ys)
    height = max(zs) - min(zs)
    wheel_radius = min(height * 0.3, width * 0.2)

    # przesun body zeby kola mialy gdzie usiasc (z=0 grunt)
    body.location.z -= min(zs) - wheel_radius

    # 4 kola w pozycjach standardowych MTA
    wheel_y = (width / 2) - wheel_radius * 0.5
    wheel_x = length / 2 - wheel_radius * 2.0
    wheel_positions = {
        "wheel_lf": ( wheel_x,  wheel_y, wheel_radius),
        "wheel_rf": ( wheel_x, -wheel_y, wheel_radius),
        "wheel_lr": (-wheel_x,  wheel_y, wheel_radius),
        "wheel_rr": (-wheel_x, -wheel_y, wheel_radius),
    }
    wheels = []
    for wn, pos in wheel_positions.items():
        bpy.ops.mesh.primitive_cylinder_add(radius=wheel_radius, depth=wheel_radius * 0.6,
                                            location=pos,
                                            rotation=(math.radians(90), 0, 0))
        w = bpy.context.active_object
        w.name = f"{final_name}_{wn}"
        wheels.append(w.name)

    # material lakieru
    paint = _make_material(f"{final_name}_paint", color, 0.8, 0.25, (0,0,0,1), 0.0)
    rubber = _make_material(f"{final_name}_rubber", (0.04, 0.04, 0.04, 1), 0.0, 0.9, (0,0,0,1), 0.0)
    if body.data.materials:
        body.data.materials[0] = paint
    else:
        body.data.materials.append(paint)
    for wn in wheels:
        bpy.data.objects[wn].data.materials.append(rubber)

    # chassis_dummy (empty) + parent
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    chassis = bpy.context.active_object
    chassis.name = f"{final_name}_chassis_dummy"
    body.parent = chassis
    body.name = f"{final_name}_body"
    for wn in wheels:
        bpy.data.objects[wn].parent = chassis

    return {"vehicle": final_name, "body": body.name, "wheels": wheels,
            "chassis_dummy": chassis.name,
            "dimensions": [round(length, 2), round(width, 2), round(height, 2)]}


# ---- apply_pbr_textures (Poly Haven full PBR) ----
def op_apply_pbr_textures(target, diffuse=None, normal=None, roughness=None,
                          ao=None, metallic=None, material_name="PBR"):
    o = _get(target)
    if material_name in bpy.data.materials:
        bpy.data.materials.remove(bpy.data.materials[material_name])
    mat = bpy.data.materials.new(material_name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")

    def _img_node(path, non_color=False):
        img = bpy.data.images.load(path, check_existing=True)
        if non_color:
            img.colorspace_settings.name = "Non-Color"
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = img
        return n

    if diffuse:
        n = _img_node(diffuse)
        nt.links.new(n.outputs["Color"], bsdf.inputs["Base Color"])
    if normal:
        n = _img_node(normal, non_color=True)
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(n.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if roughness:
        n = _img_node(roughness, non_color=True)
        nt.links.new(n.outputs["Color"], bsdf.inputs["Roughness"])
    if metallic:
        n = _img_node(metallic, non_color=True)
        nt.links.new(n.outputs["Color"], bsdf.inputs["Metallic"])
    if ao:
        # mix AO * BaseColor
        n = _img_node(ao, non_color=True)
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs["Fac"].default_value = 0.7
        if diffuse:
            # podepnij za diffuse
            for link in list(nt.links):
                if link.to_node == bsdf and link.to_socket.name == "Base Color":
                    diff_node = link.from_node
                    nt.links.remove(link)
                    nt.links.new(diff_node.outputs["Color"], mix.inputs["Color1"])
                    nt.links.new(n.outputs["Color"], mix.inputs["Color2"])
                    nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
                    break

    if o.data.materials:
        o.data.materials[0] = mat
    else:
        o.data.materials.append(mat)
    return {"applied": material_name, "to": target,
            "maps": [k for k, v in dict(diffuse=diffuse, normal=normal,
                     roughness=roughness, ao=ao, metallic=metallic).items() if v]}


# ---- raw python ----
def op_run_python(code):
    g = {"bpy": bpy, "bmesh": bmesh, "math": math, "Vector": Vector,
         "Matrix": Matrix, "Euler": Euler, "result": None}
    exec(code, g)
    val = g.get("result")
    if val is None:
        return {"ok": True}
    try:
        json.dumps(val)
        return {"result": val}
    except Exception:
        return {"result": str(val)}


# =========================================================================
#  REGISTRY
# =========================================================================
_OPS = {
    "scene_reset": op_scene_reset,
    "scene_info": op_scene_info,
    "add_primitive": op_add_primitive,
    "transform_object": op_transform_object,
    "delete_object": op_delete_object,
    "duplicate_object": op_duplicate_object,
    "boolean_op": op_boolean_op,
    "modifier_add": op_modifier_add,
    "modifier_apply_all": op_modifier_apply_all,
    "material_create": op_material_create,
    "material_assign": op_material_assign,
    "texture_load": op_texture_load,
    "texture_paint_procedural": op_texture_paint_procedural,
    "uv_unwrap": op_uv_unwrap,
    "bake_texture": op_bake_texture,
    "gen_vehicle": op_gen_vehicle,
    "gen_building": op_gen_building,
    "gen_character": op_gen_character,
    "gen_skin_variant": op_gen_skin_variant,
    "gen_weapon": op_gen_weapon,
    "light_add": op_light_add,
    "camera_set": op_camera_set,
    "render_image": op_render_image,
    "viewport_snapshot": op_viewport_snapshot,
    "viewport_set_camera_orbit": op_viewport_set_camera_orbit,
    "import_glb": op_import_glb,
    "import_fbx": op_import_fbx,
    "import_obj": op_import_obj,
    "cleanup_mesh": op_cleanup_mesh,
    "auto_rig_character": op_auto_rig_character,
    "assemble_vehicle_from_body": op_assemble_vehicle_from_body,
    "apply_pbr_textures": op_apply_pbr_textures,
    "export_dff": op_export_dff,
    "export_fbx": op_export_fbx,
    "export_obj": op_export_obj,
    "export_glb": op_export_glb,
    "run_python": op_run_python,
}


# =========================================================================
#  REGISTER
# =========================================================================
def register():
    global _SERVER, _STREAMER
    bpy.app.timers.register(_pump_queue, persistent=True)
    _SERVER = _TCPServer()
    _SERVER.start()
    _STREAMER = _PreviewStreamer()
    _STREAMER.start()
    print("[mcp-addon] registered")


def unregister():
    global _SERVER, _STREAMER
    if _SERVER:
        _SERVER.stop()
    if _STREAMER:
        _STREAMER.stop()
    print("[mcp-addon] unregistered")


if __name__ == "__main__":
    register()
