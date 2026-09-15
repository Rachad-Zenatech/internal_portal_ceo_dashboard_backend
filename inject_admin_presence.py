import shutil
import os

admin_dir = r"C:\Users\AlvinTsang\Desktop\Admin Project\internal_portal_administration_backend"
src_presence = r"C:\Users\AlvinTsang\Desktop\CEO Dashboard Project\internal_portal_ceo_dashboard_backend\services\mqtt_presence.py"
dst_presence = os.path.join(admin_dir, "services", "mqtt_presence.py")

# 1. Copy mqtt_presence.py
os.makedirs(os.path.dirname(dst_presence), exist_ok=True)
shutil.copy2(src_presence, dst_presence)
print("Copied mqtt_presence.py to Admin services")

# 2. View lifespan in admin server.py
admin_server_path = os.path.join(admin_dir, "server.py")
with open(admin_server_path, "r", encoding="utf-8") as f:
    admin_code = f.read()

# Let's inspect lifespan in admin server.py
print("Admin server lifespan snippet:")
for line in admin_code.splitlines()[65:100]:
    print(line)
