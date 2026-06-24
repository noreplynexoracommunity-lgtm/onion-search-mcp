#!/bin/sh
# Startuje headless Blendera z zaladowanym addonem MCP.
set -e

ADDON_SRC="/app/blender_mcp_addon.py"
BLENDER_CFG="$HOME/.config/blender/4.2"
ADDONS_DIR="$BLENDER_CFG/scripts/addons"
mkdir -p "$ADDONS_DIR"
cp "$ADDON_SRC" "$ADDONS_DIR/blender_mcp_addon.py"

# Opcjonalny DragonFF (do MTA .dff/.txd/.col)
if [ -f /app/DragonFF.zip ]; then
    echo "[start-blender] Instaluje DragonFF z /app/DragonFF.zip..."
    unzip -o /app/DragonFF.zip -d "$ADDONS_DIR/" > /dev/null || true
fi

cat > /tmp/_load_addon.py <<'PY'
import bpy, addon_utils, time

for a in ("DragonFF", "gta_dff", "DragonFF-master"):
    try:
        addon_utils.enable(a, default_set=True, persistent=True)
        print(f"[loader] enabled: {a}", flush=True)
    except Exception as e:
        print(f"[loader] skip {a}: {e}", flush=True)

addon_utils.enable("blender_mcp_addon", default_set=True, persistent=True)
print("[loader] enabled: blender_mcp_addon", flush=True)
bpy.ops.wm.save_userpref()

def _keepalive():
    return 5.0
bpy.app.timers.register(_keepalive, persistent=True)

print("[loader] entering keep-alive loop", flush=True)
while True:
    time.sleep(60)
PY

while true; do
    echo "[start-blender] start headless..."
    blender --background --python /tmp/_load_addon.py || true
    echo "[start-blender] blender zakonczyl -- restart za 5s"
    sleep 5
done
